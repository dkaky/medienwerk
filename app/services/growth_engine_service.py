"""Deterministische Growth-Engine: Geschaeftswahrheit, Chancen und Experimente.

Das Modul ist absichtlich frei von LLM- und Marketplace-Schreibzugriffen. Es liest
lokale ORM-Daten, persistiert erklaerbare Ableitungen und dokumentiert Owner-
Entscheidungen. Konsequente Aktionen bleiben ausserhalb dieser Schicht.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    GrowthApproval,
    GrowthExperiment,
    GrowthExperimentAssignment,
    GrowthLearning,
    GrowthListingMetric,
    GrowthOpportunity,
    GrowthScorecard,
    Listing,
    OrderAliexpress,
    Product,
    Sale,
)
from app.services.common import VOID_SALE_STATUS


COMPLETED_SALE_STATUSES = frozenset({"delivered", "self_shipped"})
CONFIRMED_COST_SOURCES = frozenset({"api", "receipt", "manual"})
CANCELED_EBAY_STATE = "CANCELED"
MIN_CONTRIBUTION_MARGIN = Decimal("0.20")
MIN_CONTRIBUTION_PER_SALE = Decimal("5.00")
SCORECARD_SCHEMA_VERSION = "1.0"

OPPORTUNITY_TYPES = frozenset({
    "WINNER_EXPANSION",
    "SUPPLIER_ECONOMICS",
    "CONVERSION_RECOVERY",
    "FEE_OPTIMIZATION",
    "DATA_QUALITY",
})
OPPORTUNITY_STATUSES = (
    "DISCOVERED",
    "ANALYZING",
    "VERIFYING",
    "READY_FOR_REVIEW",
    "APPROVED",
    "REJECTED",
    "EXPERIMENT_RUNNING",
    "WON",
    "LOST",
    "INCONCLUSIVE",
    "SCALED",
    "CLOSED",
)
TERMINAL_OPPORTUNITY_STATUSES = frozenset({"REJECTED", "SCALED", "CLOSED"})
REVIEWED_OPPORTUNITY_STATUSES = frozenset({
    "READY_FOR_REVIEW", "APPROVED", "EXPERIMENT_RUNNING"
})
EXPERIMENT_FINAL_STATUSES = frozenset({"WON", "LOST", "INCONCLUSIVE", "CLOSED"})

CONSEQUENTIAL_ACTIONS = frozenset({
    "SUPPLIER_SWITCH",
    "LISTING_CREATE",
    "LISTING_END",
    "LISTING_DELETE",
    "PRICE_CHANGE",
    "ADVERTISING_CHANGE",
    "FULFILLMENT_ACTION",
    "PURCHASE",
    "LISTING_CONTENT_CHANGE",
    "START_EXPERIMENT",
})

ALLOWED_TRANSITIONS = {
    "DISCOVERED": {"ANALYZING", "REJECTED", "CLOSED"},
    "ANALYZING": {"VERIFYING", "REJECTED", "CLOSED"},
    "VERIFYING": {"READY_FOR_REVIEW", "REJECTED", "CLOSED"},
    "READY_FOR_REVIEW": {"APPROVED", "REJECTED", "VERIFYING"},
    "APPROVED": {"EXPERIMENT_RUNNING", "CLOSED"},
    "EXPERIMENT_RUNNING": {"WON", "LOST", "INCONCLUSIVE", "CLOSED"},
    "WON": {"SCALED", "CLOSED"},
    "LOST": {"CLOSED"},
    "INCONCLUSIVE": {"VERIFYING", "CLOSED"},
    "REJECTED": {"VERIFYING", "CLOSED"},  # nur neue Evidenz darf wieder oeffnen
    "SCALED": {"CLOSED"},
    "CLOSED": set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize SQLite's timezone-naive datetimes before Python comparisons."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _money(value: Decimal | float | int) -> Decimal:
    return _d(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return (numerator / denominator).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _pct(value: Decimal | None) -> float | None:
    return round(float(value) * 100, 2) if value is not None else None


def _is_ebay_cancelled(sale: Sale) -> bool:
    return (sale.ebay_cancel_state or "").upper() == CANCELED_EBAY_STATE


def _not_ebay_cancelled_clause():
    return or_(
        Sale.ebay_cancel_state.is_(None),
        func.upper(Sale.ebay_cancel_state) != CANCELED_EBAY_STATE,
    )


def _margin_fraction(value: Decimal | float | int | None) -> Decimal | None:
    """Validate margin input as a decimal fraction: 0.30 means 30 percent."""
    if value is None:
        return None
    margin = _d(value)
    if margin <= 0 or margin > 1:
        raise ValueError(
            "expected_margin_pct must be a decimal fraction greater than 0 and at most 1 "
            "(example: 0.30 means 30%)"
        )
    return margin


def _iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def _coverage(count: int, eligible: int, revenue: Decimal, eligible_revenue: Decimal) -> dict:
    return {
        "count": int(count),
        "eligible_count": int(eligible),
        "count_pct": round(count / eligible * 100, 2) if eligible else None,
        "revenue_eur": float(_money(revenue)),
        "eligible_revenue_eur": float(_money(eligible_revenue)),
        "revenue_pct": round(float(revenue / eligible_revenue * 100), 2)
        if eligible_revenue > 0 else None,
    }


def _sale_rows(db: Session, *, start: datetime | None = None) -> list[tuple[Sale, OrderAliexpress | None]]:
    stmt = select(Sale, OrderAliexpress).outerjoin(
        OrderAliexpress, OrderAliexpress.sale_id == Sale.id
    )
    if start is not None:
        stmt = stmt.where(func.coalesce(Sale.sale_date, Sale.created_at) >= start)
    return list(db.execute(stmt).all())


def refresh_scorecard(
    db: Session, *, now: datetime | None = None, window_days: int = 30
) -> GrowthScorecard:
    """Berechnet und speichert eine statusbereinigte Scorecard.

    ``Sale.price_eur`` ist bereits der komplette Line-Total und wird nie mit der
    Menge multipliziert. Beitrag wird nur bei echter Gebuehr plus bestaetigtem
    Einkaufspreis gebildet.
    """
    now = now or _now()
    start = now - timedelta(days=window_days)
    rows = _sale_rows(db, start=start)
    valid = [
        (s, o) for s, o in rows
        if (s.status or "") not in VOID_SALE_STATUS and not _is_ebay_cancelled(s)
    ]
    completed = [(s, o) for s, o in valid if (s.status or "") in COMPLETED_SALE_STATUSES]

    valid_revenue = sum((_d(s.price_eur) for s, _ in valid if s.price_eur is not None), Decimal("0"))
    completed_revenue = sum(
        (_d(s.price_eur) for s, _ in completed if s.price_eur is not None), Decimal("0")
    )
    fee_rows = [(s, o) for s, o in completed if s.price_eur is not None and s.fee_eur_actual is not None]
    cost_rows = [
        (s, o) for s, o in completed
        if s.price_eur is not None and o is not None and o.cost_cny is not None
        and _d(o.cost_cny) > 0 and (o.cost_source or "") in CONFIRMED_COST_SOURCES
    ]
    contribution_rows = [
        (s, o) for s, o in cost_rows if s.fee_eur_actual is not None
    ]

    fee_revenue = sum((_d(s.price_eur) for s, _ in fee_rows), Decimal("0"))
    fees = sum((_d(s.fee_eur_actual) for s, _ in fee_rows), Decimal("0"))
    cost_revenue = sum((_d(s.price_eur) for s, _ in cost_rows), Decimal("0"))
    supplier_cost = sum((_d(o.cost_cny) for _, o in cost_rows if o is not None), Decimal("0"))
    contribution_revenue = sum((_d(s.price_eur) for s, _ in contribution_rows), Decimal("0"))
    contribution = sum(
        (_d(s.price_eur) - _d(s.fee_eur_actual) - _d(o.cost_cny)
         for s, o in contribution_rows if o is not None),
        Decimal("0"),
    )

    active_listings = list(db.scalars(select(Listing).where(Listing.listing_status == "active")))
    active_count = len(active_listings)
    winner_count = sum(1 for listing in active_listings if int(listing.sales_total or 0) > 0)
    mature_cutoff = now - timedelta(days=60)
    mature_no_sale = sum(
        1 for listing in active_listings
        if int(listing.sales_total or 0) == 0
        and listing.listing_start_date is not None
        and _as_utc(listing.listing_start_date) <= mature_cutoff
    )

    contribution_by_listing: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    unlinked_contribution = Decimal("0")
    for sale, order in contribution_rows:
        amount = _d(sale.price_eur) - _d(sale.fee_eur_actual) - _d(order.cost_cny if order else None)
        if sale.listing_id is None:
            unlinked_contribution += amount
        else:
            contribution_by_listing[sale.listing_id] += amount
    linked_total = sum(contribution_by_listing.values(), Decimal("0"))
    top_twenty = sum(sorted(contribution_by_listing.values(), reverse=True)[:20], Decimal("0"))
    concentration = _ratio(top_twenty, linked_total)

    coverage = {
        "status_corrected_revenue": _coverage(
            sum(1 for s, _ in valid if s.price_eur is not None), len(valid),
            valid_revenue, valid_revenue,
        ),
        "completed_revenue": _coverage(
            sum(1 for s, _ in completed if s.price_eur is not None), len(completed),
            completed_revenue, completed_revenue,
        ),
        "actual_ebay_fee": _coverage(len(fee_rows), len(completed), fee_revenue, completed_revenue),
        "confirmed_supplier_cost": _coverage(
            len(cost_rows), len(completed), cost_revenue, completed_revenue
        ),
        "completed_contribution": _coverage(
            len(contribution_rows), len(completed), contribution_revenue, completed_revenue
        ),
        "active_listing_fields": {
            "listing_count": active_count,
            "sales_total_present": sum(l.sales_total is not None for l in active_listings),
            "listing_start_date_present": sum(l.listing_start_date is not None for l in active_listings),
        },
        "winner_concentration": {
            "linked_contribution_eur": float(_money(linked_total)),
            "unlinked_contribution_eur": float(_money(unlinked_contribution)),
            "linked_listing_count": len(contribution_by_listing),
        },
    }

    contribution_cov = coverage["completed_contribution"]["revenue_pct"] or 0
    confidence = "HIGH" if contribution_cov >= 95 else "MEDIUM" if contribution_cov >= 80 else "LOW"
    provenance = {
        "calculation": "DETERMINISTIC_LOCAL_ORM",
        "source_tables": ["sales", "orders_aliexpress", "listings"],
        "completed_statuses": sorted(COMPLETED_SALE_STATUSES),
        "void_statuses": sorted(VOID_SALE_STATUS),
        "confirmed_cost_sources": sorted(CONFIRMED_COST_SOURCES),
        "line_total_semantics": "Sale.price_eur is the complete eBay line total including allocated shipping; quantity is not multiplied.",
        "contribution_formula": "line_total_eur - actual_ebay_fee_eur - confirmed_supplier_cost_eur",
        "cancellation_filter": "Void Sale.status values and ebay_cancel_state=CANCELED are excluded.",
        "known_refund_limitation": "Full refunds without a reliable Sale.status or ebay_cancel_state marker cannot yet be identified and may remain in completed economics.",
        "winner_definition": "active listing with sales_total > 0",
        "mature_no_sale_definition": "active, sales_total = 0, listing age >= 60 days",
        "winner_concentration_definition": "top 20 listing share of covered completed contribution in the period",
        "estimated_values_in_trusted_contribution": False,
    }
    scorecard = GrowthScorecard(
        period_start=start,
        period_end=now,
        calculated_at=now,
        window_days=window_days,
        status_corrected_revenue_eur=_money(valid_revenue),
        completed_revenue_eur=_money(completed_revenue),
        completed_contribution_eur=_money(contribution) if contribution_rows else None,
        contribution_margin_pct=_ratio(contribution, contribution_revenue),
        ebay_fees_eur=_money(fees) if fee_rows else None,
        ebay_fee_load_pct=_ratio(fees, fee_revenue),
        supplier_cost_eur=_money(supplier_cost) if cost_rows else None,
        supplier_cost_share_pct=_ratio(supplier_cost, cost_revenue),
        active_listings=active_count,
        winner_listings=winner_count,
        winner_concentration_pct=concentration,
        mature_no_sale_listings=mature_no_sale,
        confidence=confidence,
        coverage=coverage,
        provenance=provenance,
        schema_version=SCORECARD_SCHEMA_VERSION,
    )
    db.add(scorecard)
    db.commit()
    db.refresh(scorecard)
    return scorecard


def latest_scorecard(db: Session) -> GrowthScorecard | None:
    return db.scalar(select(GrowthScorecard).order_by(GrowthScorecard.calculated_at.desc()).limit(1))


def capture_listing_metrics(db: Session, *, now: datetime | None = None) -> dict:
    """Persistiert genau einen lokalen Listing-Stand je UTC-Tag.

    Der bestehende eBay-Sync bleibt fuer die Aktualisierung verantwortlich. Dieser
    Schritt kopiert nur den danach lokal vorliegenden Stand und ruft keine API auf.
    """
    now = now or _now()
    captured = now.replace(hour=0, minute=0, second=0, microsecond=0)
    existing = {
        row.listing_id: row for row in db.scalars(
            select(GrowthListingMetric).where(GrowthListingMetric.captured_at == captured)
        )
    }
    created = 0
    updated = 0
    for listing in db.scalars(select(Listing).where(Listing.listing_status == "active")):
        row = existing.get(listing.id)
        if row is None:
            row = GrowthListingMetric(listing_id=listing.id, captured_at=captured)
            db.add(row)
            created += 1
        else:
            updated += 1
        row.impressions = listing.impressions_week
        row.clicks = listing.views_30d if listing.views_30d is not None else listing.clicks_week
        row.sales_total = listing.sales_total
        row.listing_status = listing.listing_status
        row.source = "LOCAL_LISTING_STATE_AFTER_EXISTING_SYNC"
    db.commit()
    return {"captured_at": captured.isoformat(), "created": created, "updated": updated}


def _upsert_opportunity(db: Session, rule_key: str, values: dict) -> GrowthOpportunity:
    opportunity = db.scalar(select(GrowthOpportunity).where(GrowthOpportunity.rule_key == rule_key))
    if opportunity is None:
        opportunity = GrowthOpportunity(rule_key=rule_key, **values)
        db.add(opportunity)
        return opportunity

    if opportunity.status in REVIEWED_OPPORTUNITY_STATUSES:
        # Recurring detection may refresh factual observations, but owner/research
        # state is immutable here. In particular, never replace confirmed research,
        # expected economics, evidence gaps or a recorded decision.
        current_evidence = dict(opportunity.evidence or {})
        current_evidence["latest_detection"] = {
            "observed_at": _iso(values.get("last_evaluated_at")) or _now().isoformat(),
            "facts": values.get("evidence") or {},
        }
        opportunity.evidence = current_evidence
        opportunity.last_evaluated_at = values.get("last_evaluated_at") or _now()
        return opportunity

    preserved_terminal = opportunity.status in TERMINAL_OPPORTUNITY_STATUSES
    auto_rejected = opportunity.decision_state == "AUTO_REJECTED"
    for key, value in values.items():
        if key in {"status", "decision_state", "rejection_reason"} and preserved_terminal and not auto_rejected:
            continue
        setattr(opportunity, key, value)
    return opportunity


def _completed_listing_economics(db: Session, start: datetime) -> dict[int, dict]:
    rows = db.execute(
        select(Sale, OrderAliexpress, Listing, Product)
        .join(Listing, Sale.listing_id == Listing.id)
        .outerjoin(Product, Listing.product_id == Product.id)
        .outerjoin(OrderAliexpress, OrderAliexpress.sale_id == Sale.id)
        .where(
            func.coalesce(Sale.sale_date, Sale.created_at) >= start,
            Sale.status.in_(tuple(COMPLETED_SALE_STATUSES)),
            _not_ebay_cancelled_clause(),
        )
    ).all()
    grouped: dict[int, dict] = {}
    for sale, order, listing, product in rows:
        group = grouped.setdefault(listing.id, {
            "listing": listing,
            "product": product,
            "eligible": 0,
            "covered": 0,
            "revenue": Decimal("0"),
            "covered_revenue": Decimal("0"),
            "fees": Decimal("0"),
            "cost": Decimal("0"),
            "contributions": [],
            "cost_sources": set(),
        })
        group["eligible"] += 1
        group["revenue"] += _d(sale.price_eur)
        confirmed = (
            sale.price_eur is not None and sale.fee_eur_actual is not None
            and order is not None and order.cost_cny is not None and _d(order.cost_cny) > 0
            and (order.cost_source or "") in CONFIRMED_COST_SOURCES
        )
        if not confirmed:
            continue
        contribution = _d(sale.price_eur) - _d(sale.fee_eur_actual) - _d(order.cost_cny)
        group["covered"] += 1
        group["covered_revenue"] += _d(sale.price_eur)
        group["fees"] += _d(sale.fee_eur_actual)
        group["cost"] += _d(order.cost_cny)
        group["contributions"].append(contribution)
        group["cost_sources"].add(order.cost_source)
    return grouped


def detect_opportunities(
    db: Session, *, now: datetime | None = None, window_days: int = 30
) -> dict:
    """Wendet transparente V1-Regeln an und aktualisiert die Opportunity-Pipeline."""
    now = now or _now()
    start = now - timedelta(days=window_days)
    scorecard = latest_scorecard(db) or refresh_scorecard(db, now=now, window_days=window_days)
    grouped = _completed_listing_economics(db, start)
    seen: set[str] = set()
    created_or_updated = 0
    auto_rejected = 0

    # 1) Bewiesene Nachfrage, aber schwache bestaetigte Lieferanten-Oekonomie.
    for listing_id, group in grouped.items():
        if group["covered"] < 2 or not group["contributions"]:
            continue
        contribution = sum(group["contributions"], Decimal("0"))
        revenue = group["covered_revenue"]
        margin = _ratio(contribution, revenue)
        median_contribution = _money(Decimal(str(median(group["contributions"]))))
        if margin is None or (margin >= MIN_CONTRIBUTION_MARGIN and median_contribution >= MIN_CONTRIBUTION_PER_SALE):
            continue
        needed = max(
            Decimal("0"),
            revenue * MIN_CONTRIBUTION_MARGIN - contribution,
            Decimal(group["covered"]) * MIN_CONTRIBUTION_PER_SALE - contribution,
        )
        listing = group["listing"]
        rule_key = f"SUPPLIER_ECONOMICS:{listing_id}"
        seen.add(rule_key)
        _upsert_opportunity(db, rule_key, {
            "type": "SUPPLIER_ECONOMICS",
            "listing_id": listing_id,
            "product_id": listing.product_id,
            "title": f"Lieferantenökonomie für Listing {listing_id} verbessern",
            "summary": "Bewiesene abgeschlossene Nachfrage, aber der bestätigte Deckungsbeitrag verfehlt mindestens eine Schutzschwelle.",
            "recommended_action": "Gleichwertige Quelle mit vollständigem Deutschland-Landed-Cost und Variantenabgleich verifizieren; noch nicht wechseln.",
            "why_now": f"{group['covered']} vollständig kosten- und gebührengedeckte Abschlüsse im 30-Tage-Fenster.",
            "evidence": {
                "classification": "FACT",
                "completed_sales": group["covered"],
                "completed_revenue_eur": float(_money(revenue)),
                "completed_contribution_eur": float(_money(contribution)),
                "completed_contribution_margin_pct": _pct(margin),
                "median_contribution_per_sale_eur": float(median_contribution),
                "confirmed_cost_sources": sorted(group["cost_sources"]),
                "coverage_pct": round(group["covered"] / group["eligible"] * 100, 2),
            },
            "expected_impact": 4,
            "expected_contribution_uplift_eur": _money(needed),
            "expected_contribution_margin_pct": MIN_CONTRIBUTION_MARGIN,
            "expected_contribution_per_sale_eur": MIN_CONTRIBUTION_PER_SALE,
            "economic_provenance": {
                "current": "CONFIRMED_COMPLETED",
                "expected": "GUARDRAIL_TARGET_NOT_SUPPLIER_QUOTE",
                "estimated_mixed_with_confirmed": False,
            },
            "confidence": "HIGH",
            "risk": "HIGH" if "Kopfhörer" in (listing.category_name or "") else "MEDIUM",
            "status": "VERIFYING",
            "decision_state": "PENDING",
            "missing_evidence": [
                "verified alternative Germany delivered cost",
                "exact product and variant equivalence",
                "supplier quality and delivery evidence",
                "compliance and return evidence where applicable",
            ],
            "rejection_reason": None,
            "last_evaluated_at": now,
        })
        created_or_updated += 1

    # 2) Beitragssieger als Seeds. Ohne echten Kandidaten bleibt die Chance ANALYZING.
    winners: list[tuple[Decimal, int, dict]] = []
    for listing_id, group in grouped.items():
        if not group["contributions"] or group["covered"] != group["eligible"]:
            continue
        contribution = sum(group["contributions"], Decimal("0"))
        margin = _ratio(contribution, group["covered_revenue"])
        med = Decimal(str(median(group["contributions"])))
        if margin is not None and margin >= MIN_CONTRIBUTION_MARGIN and med >= MIN_CONTRIBUTION_PER_SALE:
            winners.append((contribution, listing_id, group))
    for contribution, listing_id, group in sorted(winners, reverse=True)[:10]:
        listing = group["listing"]
        rule_key = f"WINNER_EXPANSION:{listing_id}"
        seen.add(rule_key)
        margin = _ratio(contribution, group["covered_revenue"])
        _upsert_opportunity(db, rule_key, {
            "type": "WINNER_EXPANSION",
            "listing_id": listing_id,
            "product_id": listing.product_id,
            "title": f"Buyer-Intent um Listing {listing_id} erweitern",
            "summary": "Ein vollständig gedeckter Beitragssieger kann als Ausgangspunkt für angrenzende Käuferintents dienen.",
            "recommended_action": "Buyer-Intent und Graph-Kante benennen, dann nur nicht-duplizierte Kandidaten mit bestätigter Landed-Cost-Eignung recherchieren.",
            "why_now": "Der Seed erfüllt beide realisierten Schutzschwellen und gehört zu den stärksten Beitragslistings im aktuellen Fenster.",
            "evidence": {
                "classification": "FACT",
                "completed_sales": group["covered"],
                "completed_revenue_eur": float(_money(group["covered_revenue"])),
                "completed_contribution_eur": float(_money(contribution)),
                "completed_contribution_margin_pct": _pct(margin),
            },
            "expected_impact": 5,
            "expected_contribution_uplift_eur": None,
            "expected_contribution_margin_pct": None,
            "expected_contribution_per_sale_eur": None,
            "economic_provenance": {
                "seed": "CONFIRMED_COMPLETED",
                "candidate": "NOT_KNOWN",
                "estimated_mixed_with_confirmed": False,
            },
            "confidence": "MEDIUM",
            "risk": "MEDIUM",
            "status": "ANALYZING",
            "decision_state": "PENDING",
            "missing_evidence": [
                "named buyer intent and graph edge",
                "real candidate offer",
                "verified Germany delivered cost",
                "candidate-specific fee and contribution",
                "duplicate, policy and compliance checks",
            ],
            "rejection_reason": None,
            "last_evaluated_at": now,
        })
        created_or_updated += 1

    # 3) Reife Listings mit Traffic und ohne Sale. Schaetzungen sind klar getrennt.
    mature_cutoff = now - timedelta(days=60)
    mature = list(db.scalars(
        select(Listing).where(
            Listing.listing_status == "active",
            func.coalesce(Listing.sales_total, 0) == 0,
            Listing.listing_start_date.isnot(None),
            Listing.listing_start_date <= mature_cutoff,
        ).order_by(func.coalesce(Listing.views_30d, Listing.clicks_week, 0).desc())
    ))
    from app.services import pricing
    from app.config import get_settings
    settings = get_settings()
    for listing in mature[:50]:
        clicks = listing.views_30d if listing.views_30d is not None else listing.clicks_week
        if int(clicks or 0) <= 0:
            continue
        rule_key = f"CONVERSION_RECOVERY:{listing.id}"
        seen.add(rule_key)
        missing: list[str] = [
            "fixed-window traffic baseline",
            "three validated comparable offers",
            "single-variable diagnosis",
        ]
        model_ready = (
            listing.price_eur is not None and listing.cost_eur is not None
            and _d(listing.price_eur) > 0
        )
        modeled_contribution = None
        modeled_margin = None
        rejection_reason = None
        status = "VERIFYING"
        decision_state = "PENDING"
        if model_ready:
            price = _d(listing.price_eur)
            cost = _d(listing.cost_eur) + _d(listing.supplier_ship_eur)
            fee = price * Decimal(str(pricing.effective_fee_pct_for_listing(listing, settings=settings)))
            fee += Decimal(str(pricing.ebay_fixed_fee(settings)))
            modeled_contribution = _money(price - cost - fee)
            modeled_margin = _ratio(modeled_contribution, price)
            if modeled_margin is None or modeled_margin < MIN_CONTRIBUTION_MARGIN or modeled_contribution < MIN_CONTRIBUTION_PER_SALE:
                status = "REJECTED"
                decision_state = "AUTO_REJECTED"
                rejection_reason = "Current modeled economics fail the 20% and/or EUR 5 gate; no conversion subsidy is allowed."
                auto_rejected += 1
            else:
                missing.append("actual fee and completed contribution after intervention")
        else:
            missing.extend(["current cost and/or selling price", "modeled economic guardrail"])

        _upsert_opportunity(db, rule_key, {
            "type": "CONVERSION_RECOVERY",
            "listing_id": listing.id,
            "product_id": listing.product_id,
            "title": f"Conversion-Diagnose für Listing {listing.id}",
            "summary": "Reifes aktives Listing mit Marketplace-Traffic, aber ohne Lebenszeitverkauf.",
            "recommended_action": "Vergleichsangebote prüfen und genau eine Ursache testen; keine automatische Preis-, Titel-, Bild- oder Kategorieänderung.",
            "why_now": f"Mindestens 60 Tage aktiv und {int(clicks or 0)} gespeicherte Views/Klicks ohne Sale.",
            "evidence": {
                "classification": "FACT",
                "listing_age_days": (now - _as_utc(listing.listing_start_date)).days
                if listing.listing_start_date else None,
                "impressions": listing.impressions_week,
                "clicks_or_views": int(clicks or 0),
                "lifetime_sales": int(listing.sales_total or 0),
                "modeled_contribution_eur": float(modeled_contribution) if modeled_contribution is not None else None,
                "modeled_contribution_margin_pct": _pct(modeled_margin),
            },
            "expected_impact": 3,
            "expected_contribution_uplift_eur": None,
            "expected_contribution_margin_pct": modeled_margin,
            "expected_contribution_per_sale_eur": modeled_contribution,
            "economic_provenance": {
                "basis": "ESTIMATED_CATALOG_NOT_COMPLETED",
                "fee": "CATEGORY_AND_CURRENT_AD_RATE_MODEL",
                "cost": "CURRENT_LISTING_COST_PLUS_RECORDED_SUPPLIER_SHIPPING",
                "estimated_mixed_with_confirmed": False,
            },
            "confidence": "LOW",
            "risk": "MEDIUM",
            "status": status,
            "decision_state": decision_state,
            "missing_evidence": missing,
            "rejection_reason": rejection_reason,
            "last_evaluated_at": now,
        })
        created_or_updated += 1

    # 4) Gebuehrenoptimierung bleibt ohne Kausal-/Sale-Time-Daten unbestätigt.
    fee_key = "FEE_OPTIMIZATION:PORTFOLIO"
    seen.add(fee_key)
    _upsert_opportunity(db, fee_key, {
        "type": "FEE_OPTIMIZATION",
        "listing_id": None,
        "product_id": None,
        "title": "Anzeigengebühren kausal testen",
        "summary": "Gebühren sind materiell, aber aktuelle Ad-Rate-Bänder beweisen keine Inkrementalität.",
        "recommended_action": "Erst Sale-Time-Ad-Rate, Attribution und täglichen Traffic erfassen; danach ein owner-genehmigtes Kontroll-Experiment.",
        "why_now": "Die Scorecard weist einen realen Gebührenanteil aus, während die bestehende Datenlage Ursache und Produktmix nicht trennt.",
        "evidence": {
            "classification": "FACT",
            "completed_ebay_fee_load_pct": _pct(scorecard.ebay_fee_load_pct),
            "fee_coverage": scorecard.coverage.get("actual_ebay_fee"),
        },
        "expected_impact": 3,
        "expected_contribution_uplift_eur": None,
        "expected_contribution_margin_pct": None,
        "expected_contribution_per_sale_eur": None,
        "economic_provenance": {"current": "CONFIRMED_COMPLETED", "incrementality": "NOT_KNOWN"},
        "confidence": "LOW",
        "risk": "MEDIUM",
        "status": "ANALYZING",
        "decision_state": "PENDING",
        "missing_evidence": [
            "sale-time ad rate",
            "promoted versus organic attribution",
            "stable treatment and control cohorts",
            "completed contribution per listing-day",
        ],
        "rejection_reason": None,
        "last_evaluated_at": now,
    })
    created_or_updated += 1

    # 5) Datenqualitaet als wirtschaftliche Chance, nicht als stiller Nullwert.
    traffic_snapshots = db.scalar(select(func.count()).select_from(GrowthListingMetric)) or 0
    quality_key = "DATA_QUALITY:FIXED_TRAFFIC_HISTORY"
    seen.add(quality_key)
    _upsert_opportunity(db, quality_key, {
        "type": "DATA_QUALITY",
        "listing_id": None,
        "product_id": None,
        "title": "Feste Traffic-Zeitfenster aufbauen",
        "summary": "Early-Winner- und Conversion-Regeln benötigen datierte Traffic-Deltas statt eines überschriebenen Rollfensters.",
        "recommended_action": "Tägliche lokale Listing-Stände sammeln; keine zusätzliche Marketplace-Abfrage durch die Growth Engine.",
        "why_now": "Ohne Baseline bleiben CTR- und Vorher/Nachher-Entscheidungen unzuverlässig.",
        "evidence": {
            "classification": "FACT",
            "stored_listing_metric_snapshots": int(traffic_snapshots),
            "source": "growth_listing_metrics",
        },
        "expected_impact": 4,
        "expected_contribution_uplift_eur": None,
        "expected_contribution_margin_pct": None,
        "expected_contribution_per_sale_eur": None,
        "economic_provenance": {"basis": "DATA_COVERAGE", "monetary_impact": "NOT_KNOWN"},
        "confidence": "HIGH",
        "risk": "LOW",
        "status": "ANALYZING" if traffic_snapshots < max(1, scorecard.active_listings * 14) else "VERIFYING",
        "decision_state": "PENDING",
        "missing_evidence": ["at least 14 daily snapshots per experiment listing"],
        "rejection_reason": None,
        "last_evaluated_at": now,
    })
    created_or_updated += 1

    # Detection rules are windowed. If a previously detected dynamic condition is
    # no longer true, do not leave an obsolete card in the owner's pipeline. An
    # approved/running experiment is intentionally preserved as historical state.
    closable_statuses = {"DISCOVERED", "ANALYZING", "VERIFYING"}
    for opportunity in db.scalars(select(GrowthOpportunity).where(
        GrowthOpportunity.type.in_((
            "WINNER_EXPANSION", "SUPPLIER_ECONOMICS", "CONVERSION_RECOVERY"
        )),
        GrowthOpportunity.status.in_(tuple(closable_statuses)),
    )):
        if opportunity.rule_key in seen:
            continue
        opportunity.status = "CLOSED"
        opportunity.decision_state = "NO_LONGER_APPLICABLE"
        opportunity.rejection_reason = "Current detection window no longer meets the transparent rule."
        opportunity.last_evaluated_at = now

    db.commit()
    return {
        "evaluated_at": now.isoformat(),
        "opportunities_updated": created_or_updated,
        "auto_rejected": auto_rejected,
        "rule_keys": sorted(seen),
    }


def _economics_ready(opportunity: GrowthOpportunity) -> bool:
    provenance = opportunity.economic_provenance or {}
    return (
        opportunity.expected_contribution_margin_pct is not None
        and _d(opportunity.expected_contribution_margin_pct) >= MIN_CONTRIBUTION_MARGIN
        and opportunity.expected_contribution_per_sale_eur is not None
        and _d(opportunity.expected_contribution_per_sale_eur) >= MIN_CONTRIBUTION_PER_SALE
        and provenance.get("expected") in {"CONFIRMED_QUOTE", "CONFIRMED_COMPLETED"}
        and not opportunity.missing_evidence
    )


def reevaluate_opportunities(db: Session, *, now: datetime | None = None) -> dict:
    """Erzwingt Schutzschwellen und verhindert optimistische READY-Zustaende."""
    now = now or _now()
    changed = 0
    for opportunity in db.scalars(select(GrowthOpportunity)):
        opportunity.last_evaluated_at = now
        if opportunity.listing_id is not None:
            listing = db.get(Listing, opportunity.listing_id)
            if listing is None or listing.listing_status != "active":
                if opportunity.status not in TERMINAL_OPPORTUNITY_STATUSES:
                    opportunity.status = "CLOSED"
                    opportunity.rejection_reason = "Referenced listing is no longer active."
                    changed += 1
                continue
        if opportunity.status == "READY_FOR_REVIEW" and not _economics_ready(opportunity):
            opportunity.status = "VERIFYING"
            opportunity.decision_state = "PENDING"
            changed += 1
        if (
            opportunity.status not in TERMINAL_OPPORTUNITY_STATUSES
            and opportunity.expected_contribution_margin_pct is not None
            and opportunity.expected_contribution_per_sale_eur is not None
            and (
                _d(opportunity.expected_contribution_margin_pct) < MIN_CONTRIBUTION_MARGIN
                or _d(opportunity.expected_contribution_per_sale_eur) < MIN_CONTRIBUTION_PER_SALE
            )
        ):
            opportunity.status = "REJECTED"
            opportunity.decision_state = "AUTO_REJECTED"
            opportunity.rejection_reason = "Expected economics fail the mandatory 20% and/or EUR 5 gate."
            changed += 1
    db.commit()
    return {"evaluated_at": now.isoformat(), "changed": changed}


def apply_verified_research(
    db: Session,
    *,
    opportunity_id: int,
    evidence: dict,
    expected_margin_pct: Decimal | float | None,
    expected_contribution_per_sale_eur: Decimal | float | None,
    expected_uplift_eur: Decimal | float | None,
    missing_evidence: list[str],
    provenance: str,
) -> GrowthOpportunity:
    """Saubere Schnittstelle fuer kuenftige Codex/Claude-Research-Adapter.

    Der Adapter liefert strukturierte Fakten. Diese Funktion allein entscheidet
    deterministisch ueber READY oder REJECTED; Freitext/Prompt-Ausgaben sind nie
    Source of Truth. ``expected_margin_pct`` is a decimal fraction in ``(0, 1]``:
    ``0.30`` means 30%; ``30`` is invalid and is rejected before state mutation.
    """
    validated_margin = _margin_fraction(expected_margin_pct)
    opportunity = db.get(GrowthOpportunity, opportunity_id)
    if opportunity is None:
        raise ValueError("Opportunity not found")
    opportunity.evidence = {**(opportunity.evidence or {}), "research": evidence}
    opportunity.expected_contribution_margin_pct = (
        validated_margin
    )
    opportunity.expected_contribution_per_sale_eur = (
        _money(_d(expected_contribution_per_sale_eur))
        if expected_contribution_per_sale_eur is not None else None
    )
    opportunity.expected_contribution_uplift_eur = (
        _money(_d(expected_uplift_eur)) if expected_uplift_eur is not None else None
    )
    opportunity.missing_evidence = list(missing_evidence)
    opportunity.economic_provenance = {
        **(opportunity.economic_provenance or {}),
        "expected": provenance,
        "estimated_mixed_with_confirmed": False,
    }
    if (
        validated_margin is not None and validated_margin < MIN_CONTRIBUTION_MARGIN
    ) or (
        expected_contribution_per_sale_eur is not None
        and _d(expected_contribution_per_sale_eur) < MIN_CONTRIBUTION_PER_SALE
    ):
        opportunity.status = "REJECTED"
        opportunity.decision_state = "AUTO_REJECTED"
        opportunity.rejection_reason = "Verified alternative economics fail the mandatory guardrail."
    elif _economics_ready(opportunity):
        opportunity.status = "READY_FOR_REVIEW"
        opportunity.decision_state = "PENDING"
        opportunity.rejection_reason = None
    else:
        opportunity.status = "VERIFYING"
        opportunity.decision_state = "PENDING"
    opportunity.last_evaluated_at = _now()
    db.commit()
    db.refresh(opportunity)
    return opportunity


def transition_opportunity(
    db: Session, opportunity: GrowthOpportunity, new_status: str, *, reason: str | None = None
) -> GrowthOpportunity:
    if new_status not in OPPORTUNITY_STATUSES:
        raise ValueError("Unknown opportunity status")
    if new_status not in ALLOWED_TRANSITIONS.get(opportunity.status, set()):
        raise ValueError(f"Transition {opportunity.status} -> {new_status} is not allowed")
    if new_status == "READY_FOR_REVIEW" and not _economics_ready(opportunity):
        raise ValueError("Opportunity lacks verified economics or required evidence")
    opportunity.status = new_status
    if reason:
        opportunity.rejection_reason = reason
    opportunity.last_evaluated_at = _now()
    db.commit()
    db.refresh(opportunity)
    return opportunity


def record_opportunity_decision(
    db: Session, *, opportunity_id: int, decision: str, note: str | None = None
) -> GrowthApproval:
    opportunity = db.get(GrowthOpportunity, opportunity_id)
    if opportunity is None:
        raise ValueError("Opportunity not found")
    if decision not in {"APPROVE", "REJECT"}:
        raise ValueError("Decision must be APPROVE or REJECT")
    if decision == "APPROVE" and opportunity.status != "READY_FOR_REVIEW":
        raise ValueError("Only READY_FOR_REVIEW opportunities can be approved")
    now = _now()
    approval = GrowthApproval(
        action_type="OPPORTUNITY_REVIEW",
        entity_type="OPPORTUNITY",
        entity_id=opportunity.id,
        requested_action={
            "decision": decision,
            "recommended_action": opportunity.recommended_action,
            "execution_authorized": False,
        },
        rationale=opportunity.summary,
        status="APPROVED" if decision == "APPROVE" else "REJECTED",
        requested_at=now,
        decided_at=now,
        decided_by="OWNER_DASHBOARD",
        decision_note=note,
    )
    db.add(approval)
    if decision == "APPROVE":
        opportunity.status = "APPROVED"
        opportunity.decision_state = "APPROVED"
    else:
        opportunity.status = "REJECTED"
        opportunity.decision_state = "REJECTED"
        opportunity.rejection_reason = note or "Rejected by owner"
    opportunity.last_evaluated_at = now
    db.commit()
    db.refresh(approval)
    return approval


def create_action_approval(
    db: Session,
    *,
    action_type: str,
    entity_type: str,
    entity_id: int,
    requested_action: dict,
    rationale: str | None = None,
) -> GrowthApproval:
    """Legt eine Freigabeanforderung an, fuehrt aber niemals die Aktion aus."""
    if action_type not in CONSEQUENTIAL_ACTIONS:
        raise ValueError("Unsupported consequential action type")
    approval = GrowthApproval(
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        requested_action={**requested_action, "execution_authorized": False},
        rationale=rationale,
        status="PENDING",
        requested_at=_now(),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def decide_action_approval(
    db: Session, *, approval_id: int, approve: bool, note: str | None = None
) -> GrowthApproval:
    approval = db.get(GrowthApproval, approval_id)
    if approval is None:
        raise ValueError("Approval not found")
    if approval.status != "PENDING":
        raise ValueError("Approval has already been decided")
    approval.status = "APPROVED" if approve else "REJECTED"
    approval.decided_at = _now()
    approval.decided_by = "OWNER_DASHBOARD"
    approval.decision_note = note
    # Approval is audit evidence only. No purchase/listing/price function is called.
    db.commit()
    db.refresh(approval)
    return approval


def early_winner_signal(
    db: Session, *, listing_id: int, as_of: datetime | None = None
) -> dict:
    """Interpretiert Traffic-Deltas transparent, ohne Profit vorherzusagen."""
    as_of = as_of or _now()
    metrics = list(db.scalars(
        select(GrowthListingMetric)
        .where(GrowthListingMetric.listing_id == listing_id, GrowthListingMetric.captured_at <= as_of)
        .order_by(GrowthListingMetric.captured_at)
    ))
    if len(metrics) < 2:
        return {
            "listing_id": listing_id,
            "action": "WATCH",
            "score": None,
            "reason": "At least two dated metric snapshots are required.",
            "data_status": "NOT_KNOWN",
        }
    baseline = metrics[0]
    current = metrics[-1]
    days = max(1, (_as_utc(current.captured_at) - _as_utc(baseline.captured_at)).days)
    impressions = max(0, int(current.impressions or 0) - int(baseline.impressions or 0))
    clicks = max(0, int(current.clicks or 0) - int(baseline.clicks or 0))
    ctr = clicks / impressions if impressions else 0.0
    day14 = days < 30
    imp_mid, imp_high = (125, 250) if day14 else (250, 500)
    click_mid, click_high = (4, 10) if day14 else (8, 20)
    imp_points = 2 if impressions >= imp_high else 1 if impressions >= imp_mid else 0
    click_points = 2 if clicks >= click_high else 1 if clicks >= click_mid else 0
    ctr_points = 1 if ctr >= 0.03 else 0
    score = imp_points + click_points + ctr_points

    completed_rows = db.execute(
        select(Sale, OrderAliexpress)
        .outerjoin(OrderAliexpress, OrderAliexpress.sale_id == Sale.id)
        .where(
            Sale.listing_id == listing_id,
            Sale.status.in_(tuple(COMPLETED_SALE_STATUSES)),
            _not_ebay_cancelled_clause(),
            func.coalesce(Sale.sale_date, Sale.created_at) >= baseline.captured_at,
        )
    ).all()
    contributions = [
        _d(s.price_eur) - _d(s.fee_eur_actual) - _d(o.cost_cny)
        for s, o in completed_rows
        if s.price_eur is not None and s.fee_eur_actual is not None and o is not None
        and o.cost_cny is not None and _d(o.cost_cny) > 0
        and (o.cost_source or "") in CONFIRMED_COST_SOURCES
    ]
    revenue = sum(
        (_d(s.price_eur) for s, o in completed_rows
         if s.price_eur is not None and s.fee_eur_actual is not None and o is not None
         and o.cost_cny is not None and _d(o.cost_cny) > 0
         and (o.cost_source or "") in CONFIRMED_COST_SOURCES),
        Decimal("0"),
    )
    contribution = sum(contributions, Decimal("0"))
    margin = _ratio(contribution, revenue)
    economics_pass = bool(
        contributions and margin is not None and margin >= MIN_CONTRIBUTION_MARGIN
        and Decimal(str(median(contributions))) >= MIN_CONTRIBUTION_PER_SALE
    )
    if contributions and not economics_pass:
        action = "LOW_PRIORITY"
        reason = "Completed demand exists, but confirmed economics fail the guardrail."
    elif score >= 4 and economics_pass:
        action = "EXPAND"
        reason = "Strong traffic signal and confirmed completed economics pass both guardrails."
    elif score >= 4:
        action = "WATCH"
        reason = "Strong traffic signal; completed economics are still required before expansion."
    elif score >= 2:
        action = "OPTIMIZE"
        reason = "Mixed early signal; diagnose impressions versus CTR with one registered variable."
    elif day14:
        action = "WATCH"
        reason = "Weak day-14 signal; no kill decision is made solely at day 14."
    else:
        action = "STOP_SOURCING_SIMILAR"
        reason = "Weak day-30 signal; stop adding siblings while the owner reviews the listing."
    return {
        "listing_id": listing_id,
        "window_days": days,
        "net_impressions": impressions,
        "net_clicks": clicks,
        "ctr_pct": round(ctr * 100, 2),
        "score": score,
        "action": action,
        "reason": reason,
        "data_status": "FACT",
        "completed_sales_with_full_economics": len(contributions),
        "completed_contribution_margin_pct": _pct(margin),
        "median_contribution_eur": round(float(median(contributions)), 2) if contributions else None,
        "economics_pass": economics_pass if contributions else None,
        "known_refund_limitation": "Full refunds without a reliable Sale.status or ebay_cancel_state marker cannot yet be identified.",
    }


def create_experiment(db: Session, payload: dict) -> GrowthExperiment:
    required = ("name", "hypothesis", "intervention", "control_method", "rollback_rule")
    missing = [field for field in required if not payload.get(field)]
    if missing:
        raise ValueError("Missing experiment fields: " + ", ".join(missing))
    experiment = GrowthExperiment(
        opportunity_id=payload.get("opportunity_id"),
        name=payload["name"],
        hypothesis=payload["hypothesis"],
        target_cohort=payload.get("target_cohort") or {},
        intervention=payload["intervention"],
        control_method=payload["control_method"],
        baseline=payload.get("baseline") or {},
        observation_period_days=int(payload.get("observation_period_days") or 30),
        metrics=payload.get("metrics") or {},
        success_threshold=payload.get("success_threshold") or {
            "min_positive_listings": 1,
            "min_weighted_margin_pct": 20,
            "min_median_contribution_eur": 5,
        },
        failure_threshold=payload.get("failure_threshold") or {},
        rollback_rule=payload["rollback_rule"],
        status="DRAFT",
    )
    db.add(experiment)
    db.flush()
    for assignment in payload.get("assignments") or []:
        db.add(GrowthExperimentAssignment(
            experiment_id=experiment.id,
            listing_id=int(assignment["listing_id"]),
            cohort=assignment.get("cohort", "TREATMENT"),
            intervention_family=assignment.get("intervention_family"),
            wave=assignment.get("wave"),
            baseline_snapshot=assignment.get("baseline_snapshot") or {},
            intervention_at=assignment.get("intervention_at"),
        ))
    db.commit()
    db.refresh(experiment)
    return experiment


def start_experiment(db: Session, *, experiment_id: int) -> GrowthExperiment:
    experiment = db.get(GrowthExperiment, experiment_id)
    if experiment is None:
        raise ValueError("Experiment not found")
    approved = db.scalar(
        select(GrowthApproval).where(
            GrowthApproval.action_type == "START_EXPERIMENT",
            GrowthApproval.entity_type == "EXPERIMENT",
            GrowthApproval.entity_id == experiment_id,
            GrowthApproval.status == "APPROVED",
        ).order_by(GrowthApproval.decided_at.desc()).limit(1)
    )
    if approved is None:
        raise ValueError("Explicit owner approval to start the experiment is required")
    if experiment.status != "DRAFT":
        raise ValueError("Only DRAFT experiments can be started")
    experiment.status = "EXPERIMENT_RUNNING"
    experiment.start_date = _now()
    if experiment.opportunity_id:
        opportunity = db.get(GrowthOpportunity, experiment.opportunity_id)
        if opportunity and opportunity.status == "APPROVED":
            opportunity.status = "EXPERIMENT_RUNNING"
    db.commit()
    db.refresh(experiment)
    return experiment


def evaluate_experiments(db: Session, *, now: datetime | None = None) -> dict:
    now = now or _now()
    evaluated = 0
    closed = 0
    for experiment in db.scalars(
        select(GrowthExperiment).where(GrowthExperiment.status == "EXPERIMENT_RUNNING")
    ):
        assignments = list(db.scalars(
            select(GrowthExperimentAssignment).where(
                GrowthExperimentAssignment.experiment_id == experiment.id
            )
        ))
        treatment_ids = [a.listing_id for a in assignments if a.cohort.upper() == "TREATMENT"]
        contributions: list[Decimal] = []
        revenue = Decimal("0")
        positive_listings: set[int] = set()
        completed_count = 0
        for assignment in assignments:
            if assignment.cohort.upper() != "TREATMENT":
                continue
            start_at = assignment.intervention_at or experiment.start_date
            if start_at is None:
                continue
            rows = db.execute(
                select(Sale, OrderAliexpress)
                .outerjoin(OrderAliexpress, OrderAliexpress.sale_id == Sale.id)
                .where(
                    Sale.listing_id == assignment.listing_id,
                    Sale.status.in_(tuple(COMPLETED_SALE_STATUSES)),
                    _not_ebay_cancelled_clause(),
                    func.coalesce(Sale.sale_date, Sale.created_at) >= start_at,
                )
            ).all()
            for sale, order in rows:
                if (
                    sale.price_eur is None or sale.fee_eur_actual is None or order is None
                    or order.cost_cny is None or _d(order.cost_cny) <= 0
                    or (order.cost_source or "") not in CONFIRMED_COST_SOURCES
                ):
                    continue
                value = _d(sale.price_eur) - _d(sale.fee_eur_actual) - _d(order.cost_cny)
                contributions.append(value)
                revenue += _d(sale.price_eur)
                completed_count += 1
                if value > 0:
                    positive_listings.add(assignment.listing_id)
        contribution = sum(contributions, Decimal("0"))
        margin = _ratio(contribution, revenue)
        median_contribution = Decimal(str(median(contributions))) if contributions else None
        coverage = {
            "completed_sales_with_full_economics": completed_count,
            "treatment_listings": len(treatment_ids),
            "positive_listings": len(positive_listings),
        }
        experiment.result = {
            "evaluated_at": now.isoformat(),
            "status": "PROVISIONAL",
            "completed_revenue_eur": float(_money(revenue)),
            "completed_contribution_eur": float(_money(contribution)),
            "weighted_contribution_margin_pct": _pct(margin),
            "median_contribution_eur": round(float(median_contribution), 2)
            if median_contribution is not None else None,
            "coverage": coverage,
            "provenance": "CONFIRMED_COMPLETED_ONLY",
            "known_refund_limitation": "Full refunds without a reliable Sale.status or ebay_cancel_state marker cannot yet be identified.",
        }
        experiment.evaluated_at = now
        evaluated += 1
        if experiment.start_date is None:
            continue
        end_at = _as_utc(experiment.start_date) + timedelta(days=experiment.observation_period_days)
        if now < end_at:
            continue
        success = experiment.success_threshold or {}
        min_positive = int(success.get("min_positive_listings", 1))
        min_margin = Decimal(str(success.get("min_weighted_margin_pct", 20))) / Decimal("100")
        min_median = Decimal(str(success.get("min_median_contribution_eur", 5)))
        if (
            len(positive_listings) >= min_positive and contributions
            and margin is not None and margin >= min_margin
            and median_contribution is not None and median_contribution >= min_median
        ):
            final_status = "WON"
            conclusion = "Experiment met its registered completed-contribution thresholds."
        elif completed_count == 0:
            final_status = "INCONCLUSIVE"
            conclusion = "Observation window ended without sufficient completed economic outcomes."
        else:
            final_status = "LOST"
            conclusion = "Experiment did not meet one or more registered completed-contribution thresholds."
        experiment.status = final_status
        experiment.result["status"] = final_status
        opportunity = db.get(GrowthOpportunity, experiment.opportunity_id) if experiment.opportunity_id else None
        if opportunity and final_status in ALLOWED_TRANSITIONS.get(opportunity.status, set()):
            opportunity.status = final_status
            opportunity.last_evaluated_at = now
        existing_learning = db.scalar(select(GrowthLearning).where(GrowthLearning.experiment_id == experiment.id))
        if existing_learning is None:
            db.add(GrowthLearning(
                experiment_id=experiment.id,
                classification="FACT" if completed_count else "INFERENCE",
                conclusion=conclusion,
                evidence=experiment.result,
            ))
        closed += 1
    db.commit()
    return {"evaluated_at": now.isoformat(), "evaluated": evaluated, "finalized": closed}


def _serialize_scorecard(row: GrowthScorecard | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "period_start": _iso(row.period_start),
        "period_end": _iso(row.period_end),
        "calculated_at": _iso(row.calculated_at),
        "window_days": row.window_days,
        "status_corrected_revenue_eur": float(row.status_corrected_revenue_eur or 0),
        "completed_revenue_eur": float(row.completed_revenue_eur or 0),
        "completed_contribution_eur": float(row.completed_contribution_eur) if row.completed_contribution_eur is not None else None,
        "contribution_margin_pct": _pct(row.contribution_margin_pct),
        "ebay_fees_eur": float(row.ebay_fees_eur) if row.ebay_fees_eur is not None else None,
        "ebay_fee_load_pct": _pct(row.ebay_fee_load_pct),
        "supplier_cost_eur": float(row.supplier_cost_eur) if row.supplier_cost_eur is not None else None,
        "supplier_cost_share_pct": _pct(row.supplier_cost_share_pct),
        "active_listings": row.active_listings,
        "winner_listings": row.winner_listings,
        "winner_concentration_pct": _pct(row.winner_concentration_pct),
        "mature_no_sale_listings": row.mature_no_sale_listings,
        "confidence": row.confidence,
        "coverage": row.coverage,
        "provenance": row.provenance,
        "schema_version": row.schema_version,
    }


def serialize_opportunity(row: GrowthOpportunity) -> dict:
    return {
        "id": row.id,
        "rule_key": row.rule_key,
        "type": row.type,
        "listing_id": row.listing_id,
        "product_id": row.product_id,
        "title": row.title,
        "summary": row.summary,
        "recommended_action": row.recommended_action,
        "why_now": row.why_now,
        "evidence": row.evidence,
        "expected_impact": row.expected_impact,
        "expected_contribution_uplift_eur": float(row.expected_contribution_uplift_eur)
        if row.expected_contribution_uplift_eur is not None else None,
        "expected_contribution_margin_pct": _pct(row.expected_contribution_margin_pct),
        "expected_contribution_per_sale_eur": float(row.expected_contribution_per_sale_eur)
        if row.expected_contribution_per_sale_eur is not None else None,
        "economic_provenance": row.economic_provenance,
        "confidence": row.confidence,
        "risk": row.risk,
        "status": row.status,
        "decision_state": row.decision_state,
        "missing_evidence": row.missing_evidence or [],
        "rejection_reason": row.rejection_reason,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "last_evaluated_at": _iso(row.last_evaluated_at),
    }


def serialize_experiment(row: GrowthExperiment) -> dict:
    return {
        "id": row.id,
        "opportunity_id": row.opportunity_id,
        "name": row.name,
        "hypothesis": row.hypothesis,
        "target_cohort": row.target_cohort,
        "intervention": row.intervention,
        "control_method": row.control_method,
        "baseline": row.baseline,
        "start_date": _iso(row.start_date),
        "observation_period_days": row.observation_period_days,
        "metrics": row.metrics,
        "success_threshold": row.success_threshold,
        "failure_threshold": row.failure_threshold,
        "rollback_rule": row.rollback_rule,
        "result": row.result,
        "status": row.status,
        "evaluated_at": _iso(row.evaluated_at),
    }


def command_center(db: Session) -> dict:
    scorecard = latest_scorecard(db)
    if scorecard is None:
        scorecard = refresh_scorecard(db)
    opportunities = list(db.scalars(select(GrowthOpportunity).order_by(
        GrowthOpportunity.expected_impact.desc(), GrowthOpportunity.created_at.desc()
    )))
    pipeline_names = ["DISCOVERED", "ANALYZING", "VERIFYING", "READY_FOR_REVIEW", "EXPERIMENT_RUNNING"]
    pipeline = {name: sum(1 for o in opportunities if o.status == name) for name in pipeline_names}
    ready = [serialize_opportunity(o) for o in opportunities if o.status == "READY_FOR_REVIEW"]
    experiments = list(db.scalars(select(GrowthExperiment).order_by(GrowthExperiment.created_at.desc())))
    experiment_counts = {
        status: sum(1 for e in experiments if e.status == status)
        for status in ("EXPERIMENT_RUNNING", "WON", "LOST", "INCONCLUSIVE")
    }
    learnings = list(db.scalars(
        select(GrowthLearning).order_by(GrowthLearning.created_at.desc()).limit(10)
    ))
    pending_approvals = db.scalar(
        select(func.count()).select_from(GrowthApproval).where(GrowthApproval.status == "PENDING")
    ) or 0
    return {
        "scorecard": _serialize_scorecard(scorecard),
        "guardrails": {
            "minimum_completed_contribution_margin_pct": 20,
            "minimum_median_contribution_per_completed_sale_eur": 5,
            "confirmed_economics_required": True,
            "automatic_consequential_actions": False,
        },
        "ready_for_review": ready,
        "pipeline": pipeline,
        "experiments": {
            "counts": experiment_counts,
            "running": [serialize_experiment(e) for e in experiments if e.status == "EXPERIMENT_RUNNING"],
        },
        "recent_learning": [
            {
                "id": learning.id,
                "experiment_id": learning.experiment_id,
                "classification": learning.classification,
                "conclusion": learning.conclusion,
                "evidence": learning.evidence,
                "created_at": _iso(learning.created_at),
            }
            for learning in learnings
        ],
        "pending_approvals": int(pending_approvals),
        "empty_review_is_valid": True,
    }
