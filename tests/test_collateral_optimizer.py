"""Tests for the collateral optimization engine."""

from datetime import date
from decimal import Decimal

from src.models.database import CollateralRecord
from src.models.enums import CollateralType
from src.collateral.optimizer import AvailableAsset, optimize_collateral_pledge

D = date(2026, 6, 25)


def _record(ctype, value, haircut):
    return CollateralRecord(
        collateral_id=f"COL-{ctype.value}", counterparty_id="CM-1",
        collateral_type=ctype, value=Decimal(str(value)), haircut_pct=haircut, as_of_date=D,
    )


class TestNoShortfall:
    def test_already_sufficient_recommends_nothing(self):
        existing = [_record(CollateralType.CASH, 1_000_000, 0)]
        result = optimize_collateral_pledge(existing, [], Decimal("500000"))
        assert result.recommendations == []
        assert result.shortfall_before == Decimal("0")
        assert result.violations == []


class TestSimpleCoverage:
    def test_cash_asset_covers_shortfall_with_zero_haircut(self):
        existing = []
        pool = [AvailableAsset(CollateralType.CASH, Decimal("500000"))]
        result = optimize_collateral_pledge(existing, pool, Decimal("500000"))
        assert result.shortfall_remaining == Decimal("0")
        assert len(result.recommendations) == 1
        assert result.recommendations[0].collateral_type == CollateralType.CASH
        assert result.recommendations[0].face_value == Decimal("500000")
        assert result.violations == []

    def test_lowest_haircut_asset_chosen_first(self):
        """Given both GSEC (5% haircut) and EQUITY (30% haircut) in the pool,
        the optimizer should exhaust the cheaper GSEC capacity before equity."""
        existing = [_record(CollateralType.CASH, 10_000_000, 0)]  # plenty of cash already
        pool = [
            AvailableAsset(CollateralType.EQUITY, Decimal("1000000")),
            AvailableAsset(CollateralType.GOVERNMENT_SECURITY, Decimal("1000000")),
        ]
        result = optimize_collateral_pledge(existing, pool, Decimal("10500000"))
        assert result.recommendations[0].collateral_type == CollateralType.GOVERNMENT_SECURITY

    def test_partial_face_value_used_when_shortfall_smaller_than_asset(self):
        existing = []
        pool = [AvailableAsset(CollateralType.CASH, Decimal("1000000"))]
        result = optimize_collateral_pledge(existing, pool, Decimal("300000"))
        assert result.recommendations[0].face_value == Decimal("300000")
        assert result.shortfall_remaining == Decimal("0")


class TestConcentrationLimit:
    def test_concentration_limit_caps_non_cash_type(self):
        """GSEC alone can't cover the whole 2M shortfall without breaching
        the 10% concentration limit on the 11M target total (room = 1.1M) —
        the optimizer should cap it well below the shortfall rather than
        recommend a non-compliant mix."""
        existing = [_record(CollateralType.CASH, 9_000_000, 0)]
        pool = [AvailableAsset(CollateralType.GOVERNMENT_SECURITY, Decimal("5000000"), Decimal("5"))]
        result = optimize_collateral_pledge(existing, pool, Decimal("11000000"))
        assert result.recommendations[0].effective_value == Decimal("1100000.00")
        assert result.shortfall_remaining == Decimal("900000.00")
        assert any("insufficient" in v for v in result.violations)

    def test_compliant_mix_is_actually_compliant(self):
        """When the pool has enough cash to keep the 50% cash-minimum and
        enough GSEC to stay under the 10% concentration cap, the result
        should have zero violations."""
        existing = []
        pool = [
            AvailableAsset(CollateralType.CASH, Decimal("900000")),
            AvailableAsset(CollateralType.GOVERNMENT_SECURITY, Decimal("100000"), Decimal("5")),
        ]
        result = optimize_collateral_pledge(existing, pool, Decimal("950000"))
        assert result.violations == []
        assert result.shortfall_remaining == Decimal("0")


class TestInsufficientPool:
    def test_empty_pool_reports_full_shortfall_remaining(self):
        existing = []
        result = optimize_collateral_pledge(existing, [], Decimal("1000000"))
        assert result.recommendations == []
        assert result.shortfall_remaining == Decimal("1000000")
        assert any("insufficient" in v for v in result.violations)

    def test_pool_smaller_than_shortfall_reports_remaining(self):
        existing = []
        pool = [AvailableAsset(CollateralType.CASH, Decimal("200000"))]
        result = optimize_collateral_pledge(existing, pool, Decimal("1000000"))
        assert result.shortfall_remaining == Decimal("800000")
