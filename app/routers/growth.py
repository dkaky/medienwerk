"""Growth Command Center API.

Alle Endpunkte sind Analyse-, Register- oder Freigabe-Endpunkte. Kein Endpunkt
ruft Kauf-, Fulfillment-, Repricing-, Publishing- oder Listing-Mutationscode auf.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import GrowthApproval, GrowthExperiment, GrowthOpportunity
from app.services import growth_engine_service as growth


def _require_growth_engine_enabled() -> None:
    """Keep the complete Growth runtime unavailable while the kill switch is off."""
    if not get_settings().growth_engine_enabled:
        raise HTTPException(status_code=404, detail="Not found")


router = APIRouter(
    prefix="/api/v1/growth",
    tags=["Growth Command Center"],
    dependencies=[Depends(_require_growth_engine_enabled)],
)


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/command-center")
def command_center(db: Session = Depends(get_db)):
    return growth.command_center(db)


@router.get("/opportunities")
def opportunities(
    status: str | None = Query(default=None),
    opportunity_type: str | None = Query(default=None, alias="type"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    stmt = select(GrowthOpportunity)
    if status:
        stmt = stmt.where(GrowthOpportunity.status == status)
    if opportunity_type:
        if opportunity_type not in growth.OPPORTUNITY_TYPES:
            raise HTTPException(status_code=400, detail="Unknown opportunity type")
        stmt = stmt.where(GrowthOpportunity.type == opportunity_type)
    rows = db.scalars(
        stmt.order_by(GrowthOpportunity.expected_impact.desc(), GrowthOpportunity.created_at.desc())
        .limit(limit)
    )
    return {"items": [growth.serialize_opportunity(row) for row in rows]}


@router.get("/opportunities/{opportunity_id}")
def opportunity_detail(opportunity_id: int, db: Session = Depends(get_db)):
    row = db.get(GrowthOpportunity, opportunity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return growth.serialize_opportunity(row)


@router.post("/opportunities/{opportunity_id}/decision")
def opportunity_decision(
    opportunity_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
):
    try:
        approval = growth.record_opportunity_decision(
            db,
            opportunity_id=opportunity_id,
            decision=str(body.get("decision") or "").upper(),
            note=(str(body.get("note")).strip() if body.get("note") else None),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {
        "approval_id": approval.id,
        "status": approval.status,
        "execution_authorized": False,
        "message": "Decision recorded. No production action was executed.",
    }


@router.post("/approvals")
def create_approval(body: dict = Body(default={}), db: Session = Depends(get_db)):
    try:
        approval = growth.create_action_approval(
            db,
            action_type=str(body.get("action_type") or "").upper(),
            entity_type=str(body.get("entity_type") or "").upper(),
            entity_id=int(body.get("entity_id")),
            requested_action=body.get("requested_action") or {},
            rationale=body.get("rationale"),
        )
    except (TypeError, ValueError) as exc:
        raise _bad_request(ValueError(str(exc))) from exc
    return {
        "id": approval.id,
        "status": approval.status,
        "action_type": approval.action_type,
        "execution_authorized": False,
    }


@router.post("/approvals/{approval_id}/decision")
def decide_approval(
    approval_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
):
    decision = str(body.get("decision") or "").upper()
    if decision not in {"APPROVE", "REJECT"}:
        raise HTTPException(status_code=400, detail="Decision must be APPROVE or REJECT")
    try:
        approval = growth.decide_action_approval(
            db,
            approval_id=approval_id,
            approve=decision == "APPROVE",
            note=(str(body.get("note")).strip() if body.get("note") else None),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {
        "id": approval.id,
        "status": approval.status,
        "execution_authorized": False,
        "message": "Approval recorded. Execution remains a separate explicit action.",
    }


@router.get("/approvals")
def approvals(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    stmt = select(GrowthApproval)
    if status:
        stmt = stmt.where(GrowthApproval.status == status.upper())
    rows = list(db.scalars(stmt.order_by(GrowthApproval.created_at.desc()).limit(limit)))
    return {
        "items": [
            {
                "id": row.id,
                "action_type": row.action_type,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "requested_action": row.requested_action,
                "rationale": row.rationale,
                "status": row.status,
                "requested_at": row.requested_at.isoformat(),
                "decided_at": row.decided_at.isoformat() if row.decided_at else None,
                "decided_by": row.decided_by,
                "decision_note": row.decision_note,
                "execution_authorized": False,
            }
            for row in rows
        ]
    }


@router.get("/experiments")
def experiments(db: Session = Depends(get_db)):
    rows = db.scalars(select(GrowthExperiment).order_by(GrowthExperiment.created_at.desc()))
    return {"items": [growth.serialize_experiment(row) for row in rows]}


@router.post("/experiments")
def create_experiment(body: dict = Body(default={}), db: Session = Depends(get_db)):
    try:
        row = growth.create_experiment(db, body)
    except (TypeError, ValueError, KeyError) as exc:
        raise _bad_request(ValueError(str(exc))) from exc
    return growth.serialize_experiment(row)


@router.post("/experiments/{experiment_id}/request-start")
def request_experiment_start(experiment_id: int, db: Session = Depends(get_db)):
    if db.get(GrowthExperiment, experiment_id) is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    try:
        approval = growth.create_action_approval(
            db,
            action_type="START_EXPERIMENT",
            entity_type="EXPERIMENT",
            entity_id=experiment_id,
            requested_action={
                "operation": "MARK_EXPERIMENT_RUNNING",
                "live_listing_changes": False,
            },
            rationale="Owner approval is required before an experiment enters its observation window.",
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {"approval_id": approval.id, "status": approval.status}


@router.post("/experiments/{experiment_id}/start")
def start_experiment(experiment_id: int, db: Session = Depends(get_db)):
    try:
        row = growth.start_experiment(db, experiment_id=experiment_id)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return {
        **growth.serialize_experiment(row),
        "execution_authorized": False,
        "message": "Measurement started; no listing or marketplace action was executed.",
    }


@router.get("/early-signal/{listing_id}")
def early_signal(listing_id: int, db: Session = Depends(get_db)):
    return growth.early_winner_signal(db, listing_id=listing_id)
