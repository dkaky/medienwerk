"""Pricing-Endpoints: Verkaufspreis-Rechner (nativer Repricing-Baustein)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas import PriceBreakdownResponse, PricingCalculateRequest
from app.services import pricing

router = APIRouter(prefix="/api/v1/pricing", tags=["Pricing & Repricing"])


@router.post("/calculate", response_model=PriceBreakdownResponse)
def calculate(body: PricingCalculateRequest):
    """Verkaufspreis + Gewinn-Aufschluesselung aus Einkauf (EUR oder CNY) berechnen."""
    if body.cost_eur is None and body.price_cny is None:
        raise HTTPException(status_code=400, detail="cost_eur oder price_cny erforderlich")

    overrides = {
        k: v for k, v in {
            "fee_pct": body.fee_pct,
            "fixed_fee_eur": body.fixed_fee_eur,
            "profit_pct": body.profit_pct,
            "profit_eur": body.profit_eur,
            "min_profit_eur": body.min_profit_eur,
            "price_cents": body.price_cents,
            "min_price_eur": body.min_price_eur,
            "max_price_eur": body.max_price_eur,
        }.items() if v is not None
    }

    if body.cost_eur is not None:
        breakdown = pricing.compute_price(body.cost_eur, **overrides)
    else:
        breakdown = pricing.price_from_cny(body.price_cny, **overrides)
    return breakdown.as_dict()
