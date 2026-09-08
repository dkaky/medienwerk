"""Stable boundary between deterministic Growth Engine and future AI research.

Adapters may collect/reason about evidence, but they cannot change opportunities,
listings, suppliers or orders directly. Only structured findings cross this module;
``growth_engine_service.apply_verified_research`` remains the deterministic gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class GrowthResearchRequest:
    opportunity_id: int
    opportunity_type: str
    question: str
    required_evidence: tuple[str, ...]
    prohibited_actions: tuple[str, ...] = (
        "purchase",
        "fulfillment",
        "listing mutation",
        "price mutation",
        "supplier switch",
    )


@dataclass(frozen=True)
class GrowthResearchFinding:
    opportunity_id: int
    evidence: dict
    # Decimal fraction in (0, 1]: 0.30 means 30%; 30 is invalid.
    expected_margin_pct: float | None = None
    expected_contribution_per_sale_eur: float | None = None
    expected_uplift_eur: float | None = None
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)
    economic_provenance: str = "NOT_KNOWN"


class GrowthResearchAdapter(Protocol):
    """Interface for future Codex/OpenAI, Claude or deterministic providers."""

    def research(self, request: GrowthResearchRequest) -> GrowthResearchFinding:
        """Return structured evidence without performing consequential actions."""
        ...
