"""Growth Engine V1: trusted economics, lifecycle and safety boundaries."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from app import scheduler as scheduler_module
from app.config import Settings, get_settings
from app.database import engine
from app.models import (
    GrowthExperiment,
    GrowthExperimentAssignment,
    GrowthListingMetric,
    GrowthOpportunity,
    GrowthScorecard,
    Listing,
    OrderAliexpress,
    Product,
    Sale,
)
from app.services import growth_engine_service as growth


NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)


def _listing(db, *, suffix: str, sales_total: int = 0, price: str = "25", cost: str = "10") -> Listing:
    product = Product(
        aliexpress_url=f"https://example.invalid/{suffix}",
        aliexpress_id=f"AE-{suffix}",
        title_raw=f"Product {suffix}",
        supplier_id=f"store-{suffix}",
    )
    db.add(product)
    db.flush()
    listing = Listing(
        product_id=product.id,
        ebay_item_id=f"EBAY-{suffix}",
        title_seo=f"Listing {suffix}",
        description="Sanitized test description",
        category_name="Test category",
        listing_status="active",
        price_eur=Decimal(price),
        cost_eur=Decimal(cost),
        supplier_ship_eur=Decimal("0"),
        sales_total=sales_total,
        listing_start_date=NOW - timedelta(days=100),
    )
    db.add(listing)
    db.flush()
    return listing


def _sale(
    db,
    listing: Listing,
    *,
    tx: str,
    status: str = "delivered",
    total: str = "25",
    fee: str | None = "5",
    cost: str | None = "10",
    cost_source: str | None = "api",
    quantity: int = 1,
    ebay_cancel_state: str | None = None,
) -> Sale:
    sale = Sale(
        ebay_transaction_id=tx,
        listing_id=listing.id,
        quantity=quantity,
        price_eur=Decimal(total),
        fee_eur_actual=Decimal(fee) if fee is not None else None,
        sale_date=NOW - timedelta(days=2),
        status=status,
        ebay_cancel_state=ebay_cancel_state,
    )
    db.add(sale)
    db.flush()
    if cost is not None:
        db.add(OrderAliexpress(
            sale_id=sale.id,
            product_id=listing.product_id,
            cost_cny=Decimal(cost),
            cost_source=cost_source,
            status="delivered",
        ))
    db.flush()
    return sale


def test_growth_tables_are_created_by_existing_database_initialization():
    tables = set(inspect(engine).get_table_names())
    assert {
        "growth_scorecards",
        "growth_opportunities",
        "growth_experiments",
        "growth_experiment_assignments",
        "growth_approvals",
        "growth_listing_metrics",
        "growth_learnings",
    } <= tables


def test_scorecard_is_status_corrected_and_does_not_multiply_line_total(db):
    listing = _listing(db, suffix="scorecard", sales_total=3)
    _sale(db, listing, tx="completed-multi", total="25", fee="5.50", cost="10", quantity=3)
    _sale(db, listing, tx="self-shipped", status="self_shipped", total="20", fee="4", cost="10")
    _sale(db, listing, tx="cancelled", status="cancelled", total="99", fee="20", cost="30")
    _sale(db, listing, tx="refunded", status="refunded", total="88", fee="18", cost="20")
    _sale(db, listing, tx="pending", status="pending", total="10", fee=None, cost=None)
    _sale(
        db,
        listing,
        tx="cancelled-after-purchase",
        status="delivered",
        ebay_cancel_state="CANCELED",
        total="100",
        fee="20",
        cost="30",
    )
    db.commit()

    scorecard = growth.refresh_scorecard(db, now=NOW)

    assert scorecard.status_corrected_revenue_eur == Decimal("55.00")
    assert scorecard.completed_revenue_eur == Decimal("45.00")
    assert scorecard.ebay_fees_eur == Decimal("9.50")
    assert scorecard.supplier_cost_eur == Decimal("20.00")
    assert scorecard.completed_contribution_eur == Decimal("15.50")
    assert scorecard.contribution_margin_pct == Decimal("0.3444")
    assert scorecard.coverage["completed_contribution"]["count_pct"] == 100.0
    assert scorecard.provenance["line_total_semantics"] == "Sale.price_eur is the complete eBay line total including allocated shipping; quantity is not multiplied."
    assert "full refunds" in scorecard.provenance["known_refund_limitation"].lower()


def test_estimated_cost_is_never_mixed_into_confirmed_contribution(db):
    listing = _listing(db, suffix="coverage", sales_total=2)
    _sale(db, listing, tx="confirmed", total="20", fee="4", cost="10", cost_source="receipt")
    _sale(db, listing, tx="estimated", total="30", fee="6", cost="12", cost_source="estimate")
    db.commit()

    scorecard = growth.refresh_scorecard(db, now=NOW)

    assert scorecard.completed_revenue_eur == Decimal("50.00")
    assert scorecard.completed_contribution_eur == Decimal("6.00")
    assert scorecard.coverage["completed_contribution"]["count_pct"] == 50.0
    assert scorecard.confidence != "HIGH"


def test_detection_keeps_unverified_work_out_of_owner_review_and_auto_rejects_weak_conversion(db):
    winner = _listing(db, suffix="winner", sales_total=2)
    _sale(db, winner, tx="winner-1", total="25", fee="5", cost="10")
    _sale(db, winner, tx="winner-2", total="25", fee="5", cost="10")

    weak = _listing(db, suffix="weak", sales_total=2)
    _sale(db, weak, tx="weak-1", total="20", fee="6", cost="11")
    _sale(db, weak, tx="weak-2", total="20", fee="6", cost="11")

    mature = _listing(db, suffix="mature", price="20", cost="18")
    mature.views_30d = 25
    mature.clicks_week = 6
    db.commit()

    result = growth.detect_opportunities(db, now=NOW)
    rows = list(db.scalars(select(GrowthOpportunity)))

    assert result["auto_rejected"] == 1
    assert not [row for row in rows if row.status == "READY_FOR_REVIEW"]
    assert next(row for row in rows if row.rule_key == f"WINNER_EXPANSION:{winner.id}").status == "ANALYZING"
    assert next(row for row in rows if row.rule_key == f"SUPPLIER_ECONOMICS:{weak.id}").status == "VERIFYING"
    conversion = next(row for row in rows if row.rule_key == f"CONVERSION_RECOVERY:{mature.id}")
    assert conversion.status == "REJECTED"
    assert conversion.decision_state == "AUTO_REJECTED"

    for sale in db.scalars(select(Sale).where(Sale.listing_id == weak.id)):
        sale.status = "refunded"
    db.commit()
    growth.detect_opportunities(db, now=NOW)
    db.refresh(next(row for row in rows if row.rule_key == f"SUPPLIER_ECONOMICS:{weak.id}"))
    stale = db.scalar(select(GrowthOpportunity).where(
        GrowthOpportunity.rule_key == f"SUPPLIER_ECONOMICS:{weak.id}"
    ))
    assert stale.status == "CLOSED"
    assert stale.decision_state == "NO_LONGER_APPLICABLE"


def test_only_verified_guardrail_passing_research_becomes_ready(db):
    listing = _listing(db, suffix="research", sales_total=2)
    _sale(db, listing, tx="research-1", total="20", fee="6", cost="11")
    _sale(db, listing, tx="research-2", total="20", fee="6", cost="11")
    db.commit()
    growth.detect_opportunities(db, now=NOW)
    opportunity = db.scalar(select(GrowthOpportunity).where(GrowthOpportunity.type == "SUPPLIER_ECONOMICS"))

    still_verifying = growth.apply_verified_research(
        db,
        opportunity_id=opportunity.id,
        evidence={"delivered_cost_eur": 7.0},
        expected_margin_pct=Decimal("0.30"),
        expected_contribution_per_sale_eur=Decimal("6"),
        expected_uplift_eur=Decimal("3"),
        missing_evidence=[],
        provenance="ESTIMATED_OFFER",
    )
    assert still_verifying.status == "VERIFYING"

    ready = growth.apply_verified_research(
        db,
        opportunity_id=opportunity.id,
        evidence={"delivered_cost_eur": 7.0, "equivalence": "VERIFIED"},
        expected_margin_pct=Decimal("0.30"),
        expected_contribution_per_sale_eur=Decimal("6"),
        expected_uplift_eur=Decimal("3"),
        missing_evidence=[],
        provenance="CONFIRMED_QUOTE",
    )
    assert ready.status == "READY_FOR_REVIEW"


def test_detection_preserves_ready_research_and_confirmed_economics(db):
    listing = _listing(db, suffix="ready-preserved", sales_total=2)
    _sale(db, listing, tx="ready-preserved-1", total="20", fee="6", cost="11")
    _sale(db, listing, tx="ready-preserved-2", total="20", fee="6", cost="11")
    db.commit()
    growth.detect_opportunities(db, now=NOW)
    opportunity = db.scalar(select(GrowthOpportunity).where(
        GrowthOpportunity.rule_key == f"SUPPLIER_ECONOMICS:{listing.id}"
    ))
    growth.apply_verified_research(
        db,
        opportunity_id=opportunity.id,
        evidence={"delivered_cost_eur": 7.0, "equivalence": "VERIFIED"},
        expected_margin_pct=Decimal("0.30"),
        expected_contribution_per_sale_eur=Decimal("6"),
        expected_uplift_eur=Decimal("3"),
        missing_evidence=[],
        provenance="CONFIRMED_QUOTE",
    )
    protected = {
        "status": opportunity.status,
        "decision_state": opportunity.decision_state,
        "expected_margin": opportunity.expected_contribution_margin_pct,
        "expected_per_sale": opportunity.expected_contribution_per_sale_eur,
        "expected_uplift": opportunity.expected_contribution_uplift_eur,
        "economic_provenance": dict(opportunity.economic_provenance),
        "missing_evidence": list(opportunity.missing_evidence),
        "rejection_reason": opportunity.rejection_reason,
        "research": dict(opportunity.evidence["research"]),
    }

    growth.detect_opportunities(db, now=NOW + timedelta(days=1))
    db.refresh(opportunity)

    assert opportunity.status == protected["status"] == "READY_FOR_REVIEW"
    assert opportunity.decision_state == protected["decision_state"]
    assert opportunity.expected_contribution_margin_pct == protected["expected_margin"]
    assert opportunity.expected_contribution_per_sale_eur == protected["expected_per_sale"]
    assert opportunity.expected_contribution_uplift_eur == protected["expected_uplift"]
    assert opportunity.economic_provenance == protected["economic_provenance"]
    assert opportunity.missing_evidence == protected["missing_evidence"]
    assert opportunity.rejection_reason == protected["rejection_reason"]
    assert opportunity.evidence["research"] == protected["research"]


def test_detection_preserves_owner_approved_opportunity(db):
    listing = _listing(db, suffix="approved-preserved", sales_total=2)
    _sale(db, listing, tx="approved-preserved-1", total="20", fee="6", cost="11")
    _sale(db, listing, tx="approved-preserved-2", total="20", fee="6", cost="11")
    db.commit()
    growth.detect_opportunities(db, now=NOW)
    opportunity = db.scalar(select(GrowthOpportunity).where(
        GrowthOpportunity.rule_key == f"SUPPLIER_ECONOMICS:{listing.id}"
    ))
    growth.apply_verified_research(
        db,
        opportunity_id=opportunity.id,
        evidence={"delivered_cost_eur": 7.0, "equivalence": "VERIFIED"},
        expected_margin_pct=Decimal("0.30"),
        expected_contribution_per_sale_eur=Decimal("6"),
        expected_uplift_eur=Decimal("3"),
        missing_evidence=[],
        provenance="CONFIRMED_QUOTE",
    )
    growth.record_opportunity_decision(db, opportunity_id=opportunity.id, decision="APPROVE")
    protected_research = dict(opportunity.evidence["research"])

    growth.detect_opportunities(db, now=NOW + timedelta(days=1))
    db.refresh(opportunity)

    assert opportunity.status == "APPROVED"
    assert opportunity.decision_state == "APPROVED"
    assert opportunity.economic_provenance["expected"] == "CONFIRMED_QUOTE"
    assert opportunity.missing_evidence == []
    assert opportunity.evidence["research"] == protected_research


def test_research_margin_must_be_decimal_fraction_and_fails_without_mutation(db):
    listing = _listing(db, suffix="margin-unit", sales_total=2)
    _sale(db, listing, tx="margin-unit-1", total="20", fee="6", cost="11")
    _sale(db, listing, tx="margin-unit-2", total="20", fee="6", cost="11")
    db.commit()
    growth.detect_opportunities(db, now=NOW)
    opportunity = db.scalar(select(GrowthOpportunity).where(
        GrowthOpportunity.rule_key == f"SUPPLIER_ECONOMICS:{listing.id}"
    ))
    before = growth.serialize_opportunity(opportunity)

    with pytest.raises(ValueError, match="decimal fraction"):
        growth.apply_verified_research(
            db,
            opportunity_id=opportunity.id,
            evidence={"delivered_cost_eur": 7.0},
            expected_margin_pct=Decimal("30"),
            expected_contribution_per_sale_eur=Decimal("6"),
            expected_uplift_eur=Decimal("3"),
            missing_evidence=[],
            provenance="CONFIRMED_QUOTE",
        )
    db.refresh(opportunity)
    assert growth.serialize_opportunity(opportunity) == before


def test_owner_decision_records_approval_without_executing_marketplace_action(db):
    listing = _listing(db, suffix="approval", price="30")
    opportunity = GrowthOpportunity(
        rule_key="TEST:APPROVAL",
        type="SUPPLIER_ECONOMICS",
        listing_id=listing.id,
        title="Owner review",
        summary="Verified candidate",
        status="READY_FOR_REVIEW",
        decision_state="PENDING",
        expected_contribution_margin_pct=Decimal("0.30"),
        expected_contribution_per_sale_eur=Decimal("6"),
        economic_provenance={"expected": "CONFIRMED_QUOTE"},
        missing_evidence=[],
    )
    db.add(opportunity)
    db.commit()

    approval = growth.record_opportunity_decision(db, opportunity_id=opportunity.id, decision="APPROVE")

    assert approval.requested_action["execution_authorized"] is False
    assert opportunity.status == "APPROVED"
    assert listing.price_eur == Decimal("30")


def test_early_winner_signal_combines_age_adjusted_traffic_with_confirmed_economics(db):
    listing = _listing(db, suffix="signal", sales_total=1)
    _sale(db, listing, tx="signal-sale", total="25", fee="5", cost="10")
    db.add_all([
        GrowthListingMetric(
            listing_id=listing.id,
            captured_at=NOW - timedelta(days=30),
            impressions=100,
            clicks=2,
            sales_total=0,
            listing_status="active",
        ),
        GrowthListingMetric(
            listing_id=listing.id,
            captured_at=NOW,
            impressions=650,
            clicks=25,
            sales_total=1,
            listing_status="active",
        ),
    ])
    db.commit()

    signal = growth.early_winner_signal(db, listing_id=listing.id, as_of=NOW)

    assert signal["action"] == "EXPAND"
    assert signal["score"] == 5
    assert signal["economics_pass"] is True


def test_cancelled_after_purchase_is_excluded_from_listing_and_early_signal_economics(db):
    listing = _listing(db, suffix="cancelled-economics", sales_total=2)
    _sale(
        db,
        listing,
        tx="cancelled-economics-1",
        total="25",
        fee="5",
        cost="10",
        ebay_cancel_state="CANCELED",
    )
    _sale(
        db,
        listing,
        tx="cancelled-economics-2",
        total="25",
        fee="5",
        cost="10",
        ebay_cancel_state="CANCELED",
    )
    db.add_all([
        GrowthListingMetric(
            listing_id=listing.id,
            captured_at=NOW - timedelta(days=30),
            impressions=0,
            clicks=0,
            sales_total=0,
            listing_status="active",
        ),
        GrowthListingMetric(
            listing_id=listing.id,
            captured_at=NOW,
            impressions=600,
            clicks=30,
            sales_total=2,
            listing_status="active",
        ),
    ])
    db.commit()

    growth.detect_opportunities(db, now=NOW)
    signal = growth.early_winner_signal(db, listing_id=listing.id, as_of=NOW)

    assert db.scalar(select(GrowthOpportunity).where(
        GrowthOpportunity.rule_key == f"WINNER_EXPANSION:{listing.id}"
    )) is None
    assert signal["completed_sales_with_full_economics"] == 0
    assert signal["economics_pass"] is None
    assert signal["action"] == "WATCH"


def test_experiment_requires_approval_then_persists_result_and_learning(db):
    listing = _listing(db, suffix="experiment", sales_total=1)
    experiment = growth.create_experiment(db, {
        "name": "Staggered listing test",
        "hypothesis": "One registered offer change improves completed contribution.",
        "intervention": "Owner applies one approved title change.",
        "control_method": "Staggered multiple baseline",
        "rollback_rule": "Owner restores the registered baseline after threshold breach.",
        "observation_period_days": 30,
        "assignments": [{"listing_id": listing.id, "cohort": "TREATMENT", "wave": 1}],
    })
    with pytest.raises(ValueError, match="owner approval"):
        growth.start_experiment(db, experiment_id=experiment.id)

    approval = growth.create_action_approval(
        db,
        action_type="START_EXPERIMENT",
        entity_type="EXPERIMENT",
        entity_id=experiment.id,
        requested_action={"live_listing_changes": False},
    )
    growth.decide_action_approval(db, approval_id=approval.id, approve=True)
    growth.start_experiment(db, experiment_id=experiment.id)
    experiment.start_date = NOW - timedelta(days=31)
    assignment = db.scalar(select(GrowthExperimentAssignment).where(
        GrowthExperimentAssignment.experiment_id == experiment.id
    ))
    assignment.intervention_at = NOW - timedelta(days=31)
    _sale(db, listing, tx="experiment-sale", total="25", fee="5", cost="10")
    _sale(
        db,
        listing,
        tx="experiment-cancelled-after-purchase",
        total="100",
        fee="20",
        cost="30",
        ebay_cancel_state="CANCELED",
    )
    db.commit()

    result = growth.evaluate_experiments(db, now=NOW)
    db.refresh(experiment)

    assert result["finalized"] == 1
    assert experiment.status == "WON"
    assert experiment.result["provenance"] == "CONFIRMED_COMPLETED_ONLY"
    assert experiment.result["completed_revenue_eur"] == 25.0
    assert experiment.result["coverage"]["completed_sales_with_full_economics"] == 1
    assert "full refunds" in experiment.result["known_refund_limitation"].lower()


def test_command_center_api_exposes_guardrails_and_valid_empty_review_state(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "growth_engine_enabled", True)
    assert client.get("/health").json()["growth_engine_enabled"] is True
    response = client.get("/api/v1/growth/command-center")
    assert response.status_code == 200
    payload = response.json()
    assert payload["guardrails"]["minimum_completed_contribution_margin_pct"] == 20
    assert payload["guardrails"]["minimum_median_contribution_per_completed_sale_eur"] == 5
    assert payload["guardrails"]["automatic_consequential_actions"] is False
    assert payload["empty_review_is_valid"] is True


def test_growth_engine_flag_defaults_off_and_hides_ui_contract():
    field = Settings.model_fields.get("growth_engine_enabled")
    assert field is not None and field.default is False
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'data-view="growth" hidden' in html
    assert "let growthEnabled = false" in html


def test_disabled_growth_api_does_not_create_runtime_state(client, db):
    assert get_settings().growth_engine_enabled is False
    assert client.get("/health").json()["growth_engine_enabled"] is False
    response = client.get("/api/v1/growth/command-center")
    assert response.status_code == 404
    db.expire_all()
    assert db.scalar(select(GrowthScorecard)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("job_name", [
    "_growth_scorecard_job",
    "_growth_opportunity_detection_job",
    "_growth_opportunity_reevaluation_job",
    "_growth_experiment_evaluation_job",
])
async def test_disabled_growth_scheduler_execution_is_noop(monkeypatch, job_name):
    assert get_settings().growth_engine_enabled is False
    monkeypatch.setattr(
        scheduler_module,
        "SessionLocal",
        lambda: pytest.fail("disabled Growth job opened a database session"),
    )
    await getattr(scheduler_module, job_name)()


def test_scheduler_does_not_register_growth_jobs_when_disabled():
    assert get_settings().growth_engine_enabled is False
    with _running_scheduler() as scheduler:
        ids = {job.id for job in scheduler.get_jobs()}
        assert not {job_id for job_id in ids if job_id.startswith("growth_")}


def test_scheduler_registers_all_growth_jobs_when_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "growth_engine_enabled", True)
    with _running_scheduler() as scheduler:
        ids = {job.id for job in scheduler.get_jobs()}
        assert {
            "growth_scorecard_refresh",
            "growth_opportunity_detection",
            "growth_opportunity_reevaluation",
            "growth_experiment_evaluation",
        } <= ids


@contextmanager
def _running_scheduler():
    scheduler_module._scheduler = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop.run_until_complete(_start_scheduler())
    finally:
        scheduler_module.shutdown_scheduler()
        loop.close()
        asyncio.set_event_loop(None)


async def _start_scheduler():
    return scheduler_module.start_scheduler()
