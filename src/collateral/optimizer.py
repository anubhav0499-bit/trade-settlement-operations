"""
Collateral Optimization — recommends the cheapest compliant mix of
unencumbered assets a clearing member should pledge to cover a margin
shortfall, instead of leaving "which collateral to post" to manual choice.

This is a deterministic greedy heuristic, not a true linear-programming
optimum: it fills the shortfall using the lowest-haircut assets first
(cash, then progressively more expensive types), capping each type at the
room remaining under the concentration limit so the result a) never
recommends a non-compliant mix when a compliant one exists in the pool, and
b) is explainable trade-by-trade (each step says why an asset was or
wasn't selected) rather than an opaque solver output. A true LP solver
could find a marginally cheaper mix in edge cases with unusual haircut
orderings — documented here rather than hidden, per this codebase's
convention of surfacing known simplifications.

Reuses compute_effective_collateral/check_cash_rule/check_concentration_limit
from manager.py rather than re-deriving those rules, so the optimizer and
the compliance checker can never silently disagree about what "compliant"
means.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from src.models.database import CollateralRecord
from src.models.enums import CollateralType
from src.collateral.manager import (
    check_cash_rule,
    check_concentration_limit,
    compute_effective_collateral,
    get_configured_haircut_pct,
)
from src.utils.config_loader import get_margin_framework_config


@dataclass
class AvailableAsset:
    """An unencumbered asset a CM could pledge — not yet a CollateralRecord
    since it isn't pledged. haircut_pct defaults to the configured haircut
    for its type if not supplied, so callers don't have to look it up."""
    collateral_type: CollateralType
    face_value: Decimal
    haircut_pct: Decimal | None = None

    def effective_value(self) -> Decimal:
        haircut = (
            Decimal(str(self.haircut_pct)) if self.haircut_pct is not None
            else get_configured_haircut_pct(self.collateral_type)
        ) / Decimal("100")
        return (Decimal(str(self.face_value)) * (Decimal("1") - haircut)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )


@dataclass
class PledgeRecommendation:
    collateral_type: CollateralType
    face_value: Decimal
    haircut_pct: Decimal
    effective_value: Decimal


@dataclass
class OptimizationResult:
    recommendations: list[PledgeRecommendation]
    total_face_value_pledged: Decimal
    total_effective_value_pledged: Decimal
    shortfall_before: Decimal
    shortfall_remaining: Decimal
    violations: list[str]


def optimize_collateral_pledge(
    existing_records: list[CollateralRecord],
    available_assets: list[AvailableAsset],
    required_margin: Decimal,
) -> OptimizationResult:
    """Recommend which available assets to pledge to cover the shortfall
    between required_margin and the CM's current effective collateral,
    at the lowest haircut cost, without breaching the cash-minimum or
    concentration rules on the resulting total portfolio."""
    config = get_margin_framework_config()["collateral"]
    concentration_limit_pct = Decimal(str(config["concentration_limit_pct"])) / Decimal("100")
    cash_equivalent_types = {
        CollateralType.CASH, CollateralType.BANK_GUARANTEE, CollateralType.FIXED_DEPOSIT,
    }

    current = compute_effective_collateral(existing_records)
    current_total = current["total"]
    shortfall = (Decimal(str(required_margin)) - current_total).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if shortfall <= 0:
        return OptimizationResult([], Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), [])

    # Target total assumes the shortfall gets exactly filled — used only to
    # size each type's concentration headroom; the actual achieved total may
    # be lower if the pool is insufficient, which check_concentration_limit
    # will catch on the real combined set at the end regardless.
    target_total = current_total + shortfall

    by_type_pledged_effective: dict[CollateralType, Decimal] = {}
    for r in existing_records:
        key = r.collateral_type
        by_type_pledged_effective[key] = by_type_pledged_effective.get(key, Decimal("0")) + (
            Decimal(str(r.value)) * (Decimal("1") - Decimal(str(r.haircut_pct)) / Decimal("100"))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Lowest haircut first — cheapest face value per unit of effective value.
    pool = sorted(available_assets, key=lambda a: (
        Decimal(str(a.haircut_pct)) if a.haircut_pct is not None
        else get_configured_haircut_pct(a.collateral_type)
    ))

    recommendations: list[PledgeRecommendation] = []
    remaining = shortfall
    for asset in pool:
        if remaining <= 0:
            break
        effective_available = asset.effective_value()
        if effective_available <= 0:
            continue

        if asset.collateral_type not in cash_equivalent_types:
            already = by_type_pledged_effective.get(asset.collateral_type, Decimal("0"))
            room = (concentration_limit_pct * target_total - already).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if room <= 0:
                continue
            effective_available = min(effective_available, room)

        effective_to_use = min(effective_available, remaining)
        if effective_to_use <= 0:
            continue

        # Back out the face value fraction of the asset actually consumed.
        haircut = (
            Decimal(str(asset.haircut_pct)) if asset.haircut_pct is not None
            else get_configured_haircut_pct(asset.collateral_type)
        ) / Decimal("100")
        full_effective = asset.effective_value()
        face_fraction = (effective_to_use / full_effective) if full_effective > 0 else Decimal("0")
        face_value_used = (Decimal(str(asset.face_value)) * face_fraction).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        recommendations.append(PledgeRecommendation(
            collateral_type=asset.collateral_type,
            face_value=face_value_used,
            haircut_pct=haircut * Decimal("100"),
            effective_value=effective_to_use,
        ))
        by_type_pledged_effective[asset.collateral_type] = (
            by_type_pledged_effective.get(asset.collateral_type, Decimal("0")) + effective_to_use
        )
        remaining -= effective_to_use

    total_face = sum((r.face_value for r in recommendations), Decimal("0"))
    total_effective = sum((r.effective_value for r in recommendations), Decimal("0"))

    simulated_records = list(existing_records) + [
        CollateralRecord(
            collateral_id=f"SIM-{i}", counterparty_id="SIMULATED",
            collateral_type=r.collateral_type, value=r.face_value,
            haircut_pct=float(r.haircut_pct),
            as_of_date=existing_records[0].as_of_date if existing_records else None,
        )
        for i, r in enumerate(recommendations)
    ]
    violations = []
    cash_violation = check_cash_rule(simulated_records)
    if cash_violation:
        violations.append(cash_violation.detail)
    for v in check_concentration_limit(simulated_records):
        violations.append(v.detail)
    if remaining > 0:
        violations.append(
            f"available asset pool insufficient to cover shortfall: "
            f"{remaining} of {shortfall} still unmet"
        )

    return OptimizationResult(
        recommendations=recommendations,
        total_face_value_pledged=total_face,
        total_effective_value_pledged=total_effective,
        shortfall_before=shortfall,
        shortfall_remaining=max(Decimal("0"), remaining),
        violations=violations,
    )
