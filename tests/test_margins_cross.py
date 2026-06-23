"""Tests for the cross margin module."""

from decimal import Decimal

from src.margins.cross_margin import apply_cross_margin, compute_cross_margin_benefit


class TestComputeCrossMarginBenefit:
    def test_no_benefit_when_not_hedged(self):
        benefit = compute_cross_margin_benefit(Decimal("10000"), Decimal("8000"), is_hedged=False)
        assert benefit == Decimal("0")

    def test_benefit_based_on_smaller_leg(self):
        benefit = compute_cross_margin_benefit(Decimal("10000"), Decimal("8000"), is_hedged=True)
        # 60% of smaller leg (8000) = 4800
        assert benefit == Decimal("4800.00")


class TestApplyCrossMargin:
    def test_subtracts_benefit_from_total(self):
        net = apply_cross_margin(Decimal("18000"), Decimal("4800"))
        assert net == Decimal("13200")

    def test_floors_at_zero(self):
        net = apply_cross_margin(Decimal("3000"), Decimal("4800"))
        assert net == Decimal("0")
