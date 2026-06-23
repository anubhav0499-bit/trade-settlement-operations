"""
Collateral Manager — effective collateral valuation, haircuts, and the
two NSE Clearing portfolio-level rules: minimum 50% cash composition and a
per-collateral-type concentration limit.

CollateralRecord models collateral at the type level (cash, bank guarantee,
FDR, G-Sec, equity) rather than per individual security, so the
concentration check here operates at that same granularity.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from src.models.database import CollateralRecord
from src.models.enums import CollateralType
from src.utils.config_loader import get_margin_framework_config


@dataclass
class CollateralViolation:
    rule: str
    detail: str


def effective_value(record: CollateralRecord) -> Decimal:
    """Collateral value net of its haircut."""
    haircut = Decimal(str(record.haircut_pct)) / Decimal("100")
    return (Decimal(str(record.value)) * (Decimal("1") - haircut)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def compute_effective_collateral(records: list[CollateralRecord]) -> dict:
    """Total effective collateral and a breakdown by type."""
    by_type: dict[str, Decimal] = {}
    total = Decimal("0")
    for r in records:
        value = effective_value(r)
        key = r.collateral_type.value if hasattr(r.collateral_type, "value") else str(r.collateral_type)
        by_type[key] = by_type.get(key, Decimal("0")) + value
        total += value
    return {"total": total, "by_type": by_type}


def check_cash_rule(records: list[CollateralRecord]) -> CollateralViolation | None:
    """Cash must be >= the configured minimum percentage of total effective collateral."""
    config = get_margin_framework_config()["collateral"]
    min_cash_pct = Decimal(str(config["min_cash_pct"])) / Decimal("100")

    breakdown = compute_effective_collateral(records)
    total = breakdown["total"]
    if total == 0:
        return None

    cash_value = breakdown["by_type"].get(CollateralType.CASH.value, Decimal("0"))
    cash_pct = cash_value / total
    if cash_pct < min_cash_pct:
        return CollateralViolation(
            "MIN_CASH",
            f"cash is {cash_pct * 100:.2f}% of effective collateral, below required {min_cash_pct * 100:.2f}%",
        )
    return None


_CASH_EQUIVALENT_TYPES = {
    CollateralType.CASH.value,
    CollateralType.BANK_GUARANTEE.value,
    CollateralType.FIXED_DEPOSIT.value,
}


def check_concentration_limit(records: list[CollateralRecord]) -> list[CollateralViolation]:
    """No single market-risk-bearing collateral type (G-Sec, equity) may exceed the
    configured concentration limit. Cash and cash-equivalents are exempt — the
    50% minimum cash rule already governs those."""
    config = get_margin_framework_config()["collateral"]
    limit_pct = Decimal(str(config["concentration_limit_pct"])) / Decimal("100")

    breakdown = compute_effective_collateral(records)
    total = breakdown["total"]
    if total == 0:
        return []

    violations = []
    for collateral_type, value in breakdown["by_type"].items():
        if collateral_type in _CASH_EQUIVALENT_TYPES:
            continue
        pct = value / total
        if pct > limit_pct:
            violations.append(
                CollateralViolation(
                    "CONCENTRATION",
                    f"{collateral_type} is {pct * 100:.2f}% of effective collateral, above limit {limit_pct * 100:.2f}%",
                )
            )
    return violations


def get_configured_haircut_pct(collateral_type: CollateralType) -> Decimal:
    config = get_margin_framework_config()["collateral"]
    return Decimal(str(config["haircuts_pct"][collateral_type.value]))
