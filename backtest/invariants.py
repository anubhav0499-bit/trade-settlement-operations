"""
Structural integrity checks for the backtest harness.

Each function returns a list of human-readable violation strings (empty list
= invariant held). These are algebraic identities and conservation laws the
settlement engines must satisfy regardless of input — a violation here means
a real bug in the engine, not a data-quality issue.
"""

from collections import defaultdict
from decimal import Decimal

from src.models.database import CollateralRecord, Obligation
from src.models.enums import NetDirection

# Per-ISIN-per-day rounding tolerance for net_value conservation: VWAP is
# quantized to 4dp per counterparty group, then net_value to 2dp, so a few
# cents of slack per group is expected, not a bug.
VALUE_TOLERANCE_PER_GROUP = Decimal("0.02")


def check_netting_conservation(obligations: list[Obligation]) -> list[str]:
    """For every (ISIN, settlement_date), the market was constructed as
    matched buy/sell pairs, so the signed net quantity and net value summed
    across all counterparties must be zero (within rounding tolerance)."""
    violations = []
    by_key: dict[tuple, list[Obligation]] = defaultdict(list)
    for ob in obligations:
        by_key[(ob.isin, ob.settlement_date)].append(ob)

    for (isin, settle_date), obs in by_key.items():
        signed_qty = sum(
            ob.net_quantity if ob.net_direction == NetDirection.PAY_OUT else -ob.net_quantity
            for ob in obs
        )
        signed_value = sum(
            (Decimal(str(ob.net_value)) if ob.net_direction == NetDirection.PAY_OUT
             else -Decimal(str(ob.net_value)))
            for ob in obs
        )
        if signed_qty != 0:
            violations.append(
                f"netting conservation broken: {isin} on {settle_date} — "
                f"signed net quantity across all counterparties = {signed_qty}, expected 0"
            )
        tolerance = VALUE_TOLERANCE_PER_GROUP * len(obs)
        if abs(signed_value) > tolerance:
            violations.append(
                f"netting conservation broken: {isin} on {settle_date} — "
                f"signed net value across all counterparties = {signed_value}, "
                f"expected within {tolerance} of 0"
            )
    return violations


def check_margin_nonnegative(label: str, *amounts: Decimal) -> list[str]:
    """No margin component should ever be negative."""
    violations = []
    for amt in amounts:
        if amt < 0:
            violations.append(f"{label}: negative margin component {amt}")
    return violations


def check_collateral_concentration(
    cm_id: str, records: list[CollateralRecord], violations_found: list, expect_violation: bool
) -> list[str]:
    """Verify the concentration check fires exactly when the seeded data
    warrants it — not always, not never."""
    fired = len(violations_found) > 0
    if expect_violation and not fired:
        return [f"{cm_id}: expected a concentration violation but check_concentration_limit found none"]
    if not expect_violation and fired:
        return [f"{cm_id}: unexpected concentration violation(s) on a compliant portfolio: {violations_found}"]
    return []


def check_waterfall_conservation(shortfall: Decimal, summary: dict) -> list[str]:
    """The waterfall must account for the entire shortfall: covered + remaining == original."""
    violations = []
    total = summary["total_covered"] + summary["final_shortfall"]
    if total != shortfall:
        violations.append(
            f"waterfall conservation broken: covered ({summary['total_covered']}) + "
            f"final_shortfall ({summary['final_shortfall']}) = {total}, expected {shortfall}"
        )
    if summary["final_shortfall"] < 0:
        violations.append(f"waterfall final_shortfall is negative: {summary['final_shortfall']}")
    if summary["total_covered"] < 0:
        violations.append(f"waterfall total_covered is negative: {summary['total_covered']}")
    return violations


def check_cm_aggregation(
    parent_id: str, aggregated_value: Decimal, independently_summed_value: Decimal
) -> list[str]:
    """Cross-check aggregate_obligations' SQL-side sum against an
    independently computed sum over the same descendant set."""
    if aggregated_value != independently_summed_value:
        return [
            f"CM aggregation mismatch for {parent_id}: aggregate_obligations returned "
            f"{aggregated_value}, independent sum is {independently_summed_value}"
        ]
    return []
