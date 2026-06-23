"""Tests for the collateral manager — effective valuation, cash rule, concentration limit."""

import uuid
from datetime import date
from decimal import Decimal

from src.models.database import CollateralRecord
from src.models.enums import CollateralType
from src.collateral.manager import (
    check_cash_rule,
    check_concentration_limit,
    compute_effective_collateral,
    effective_value,
    get_configured_haircut_pct,
)


def _record(collateral_type, value, haircut_pct=0.0):
    return CollateralRecord(
        collateral_id=str(uuid.uuid4()),
        counterparty_id="BRK-001",
        collateral_type=collateral_type,
        value=Decimal(str(value)),
        haircut_pct=haircut_pct,
        as_of_date=date(2026, 6, 1),
    )


class TestEffectiveValue:
    def test_applies_haircut(self):
        r = _record(CollateralType.EQUITY, 100000, haircut_pct=30.0)
        assert effective_value(r) == Decimal("70000.00")

    def test_zero_haircut_returns_full_value(self):
        r = _record(CollateralType.CASH, 50000, haircut_pct=0.0)
        assert effective_value(r) == Decimal("50000.00")


class TestComputeEffectiveCollateral:
    def test_totals_and_breakdown(self):
        records = [
            _record(CollateralType.CASH, 60000, 0.0),
            _record(CollateralType.EQUITY, 40000, 30.0),
        ]
        result = compute_effective_collateral(records)
        assert result["total"] == Decimal("88000.00")
        assert result["by_type"]["CASH"] == Decimal("60000.00")
        assert result["by_type"]["EQUITY"] == Decimal("28000.00")


class TestCheckCashRule:
    def test_passes_when_cash_meets_minimum(self):
        records = [
            _record(CollateralType.CASH, 60000, 0.0),
            _record(CollateralType.EQUITY, 40000, 0.0),
        ]
        assert check_cash_rule(records) is None

    def test_fails_when_cash_below_minimum(self):
        records = [
            _record(CollateralType.CASH, 30000, 0.0),
            _record(CollateralType.EQUITY, 70000, 0.0),
        ]
        violation = check_cash_rule(records)
        assert violation is not None
        assert violation.rule == "MIN_CASH"

    def test_empty_records_returns_none(self):
        assert check_cash_rule([]) is None


class TestCheckConcentrationLimit:
    def test_equity_over_limit_flagged(self):
        records = [
            _record(CollateralType.CASH, 50000, 0.0),
            _record(CollateralType.EQUITY, 50000, 0.0),
        ]
        violations = check_concentration_limit(records)
        assert len(violations) == 1
        assert violations[0].rule == "CONCENTRATION"

    def test_cash_heavy_portfolio_not_flagged_for_cash(self):
        records = [_record(CollateralType.CASH, 100000, 0.0)]
        violations = check_concentration_limit(records)
        assert violations == []

    def test_within_limit_not_flagged(self):
        records = [
            _record(CollateralType.CASH, 92000, 0.0),
            _record(CollateralType.EQUITY, 8000, 0.0),
        ]
        assert check_concentration_limit(records) == []


class TestGetConfiguredHaircutPct:
    def test_equity_haircut(self):
        assert get_configured_haircut_pct(CollateralType.EQUITY) == Decimal("30.0")

    def test_cash_haircut_is_zero(self):
        assert get_configured_haircut_pct(CollateralType.CASH) == Decimal("0.0")
