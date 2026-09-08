# POD Shop — Product Context

## Product

- **Platform:** Private web application for one e-commerce owner/operator.
- **Purpose:** Operate an eBay business safely and turn trusted business data into explainable growth decisions.
- **Primary user:** The owner, who needs a compact operational view and retains final authority over consequential marketplace and purchasing actions.
- **Operating context:** A real production business with eBay listings, supplier orders, fulfillment, fees, accounting, and limited owner attention.

## Growth Command Center

The Growth Command Center is the owner's persistent decision surface. It must:

- show status-corrected, provenance-carrying business metrics;
- distinguish confirmed economics from estimates and missing data;
- surface only opportunities that pass deterministic evidence and economic gates;
- accept that zero review-ready opportunities is a healthy valid state;
- make every recommendation explainable;
- register approvals and experiments without silently executing marketplace actions;
- expose pipeline state, experiment outcomes, and recent learning.

## Product Truth and Safety

- `Sale.price_eur` is the complete eBay line-item total, including allocated shipping; quantity is not multiplied.
- Completed economics use only completed sales with actual eBay fees and confirmed supplier cost sources.
- The minimum growth gate is at least 20% expected completed contribution margin and at least EUR 5 expected contribution per completed sale.
- Estimated and confirmed economics must never be silently mixed.
- No automatic purchasing, fulfillment, repricing, listing deletion/ending, supplier switching, advertising changes, listing creation, or material listing-content change is allowed in Growth Engine V1.
- Owner approval records are auditable decisions, not authorization for hidden execution.
- The database and deterministic opportunity engine remain the source of truth; future AI research is an adapter, not the decision authority.

## Existing Visual World

- Extend the incumbent POD Shop interface and its restrained dark-green/brass visual system.
- Use the current navigation, typography, controls, cards, status colors, and responsive behavior rather than introducing a separate design language.
- Growth belongs inside the existing Optimierung area in both maintained dashboard designs before owner release; the current standalone shadow surface stays hidden while the feature flag is off.
- Owner-facing copy is concise German. Evidence classifications and lifecycle status may retain stable machine-readable names where useful.

## Interaction Principles

1. **Business truth before action.** Put coverage, provenance, and uncertainty next to the metric or recommendation they qualify.
2. **Decision clarity.** A review card answers why now, expected effect, confidence, risk, supporting evidence, and remaining uncertainty.
3. **Safe by construction.** Approval is explicit, consequential execution is separate, and weak evidence remains unverified or is rejected.
4. **Low owner load.** Daily analysis is scheduled; the owner mainly reviews strong decisions and experiment outcomes.

## Accessibility and Responsiveness

- Preserve semantic buttons and visible keyboard focus.
- Keep body text readable and state meaning available in text, not color alone.
- Collapse dense dashboard regions structurally on small screens without hiding provenance or approval meaning.
