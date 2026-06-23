"""Tests for day-count conventions and clean/dirty price conversion."""

from datetime import date
from decimal import Decimal

from src.models.enums import DayCountConvention
from src.debt.accrued_interest import (
    clean_to_dirty_price,
    compute_accrued_interest,
    day_count_fraction,
    dirty_to_clean_price,
)


class TestDayCountFraction:
    def test_thirty_360_half_year(self):
        # Jan 1 -> Jul 1 = 6 months = 180/360 = 0.5
        frac = day_count_fraction(date(2026, 1, 1), date(2026, 7, 1), DayCountConvention.THIRTY_360)
        assert frac == Decimal("0.5")

    def test_actual_365_full_year(self):
        frac = day_count_fraction(date(2025, 1, 1), date(2026, 1, 1), DayCountConvention.ACTUAL_365)
        assert abs(frac - Decimal("1")) < Decimal("0.01")

    def test_actual_actual_full_year(self):
        frac = day_count_fraction(date(2025, 1, 1), date(2026, 1, 1), DayCountConvention.ACTUAL_ACTUAL)
        assert abs(frac - Decimal("1")) < Decimal("0.01")


class TestComputeAccruedInterest:
    def test_half_year_accrual_30_360(self):
        accrued = compute_accrued_interest(
            face_value=Decimal("100000"),
            coupon_rate_pct=Decimal("8"),
            last_coupon_date=date(2026, 1, 1),
            settlement_date=date(2026, 7, 1),
            day_count_convention=DayCountConvention.THIRTY_360,
        )
        # 100000 * 8% * 0.5 = 4000
        assert accrued == Decimal("4000.00")

    def test_zero_days_yields_zero_accrual(self):
        accrued = compute_accrued_interest(
            face_value=Decimal("100000"),
            coupon_rate_pct=Decimal("8"),
            last_coupon_date=date(2026, 1, 1),
            settlement_date=date(2026, 1, 1),
            day_count_convention=DayCountConvention.THIRTY_360,
        )
        assert accrued == Decimal("0.00")


class TestCleanDirtyConversion:
    def test_clean_to_dirty_adds_accrued(self):
        dirty = clean_to_dirty_price(
            clean_price=Decimal("98.50"),
            face_value=Decimal("100"),
            coupon_rate_pct=Decimal("8"),
            last_coupon_date=date(2026, 1, 1),
            settlement_date=date(2026, 7, 1),
            day_count_convention=DayCountConvention.THIRTY_360,
        )
        # accrued per 100 face = 100 * 8% * 0.5 = 4.00
        assert dirty == Decimal("102.50")

    def test_dirty_to_clean_is_inverse(self):
        clean = dirty_to_clean_price(
            dirty_price=Decimal("102.50"),
            face_value=Decimal("100"),
            coupon_rate_pct=Decimal("8"),
            last_coupon_date=date(2026, 1, 1),
            settlement_date=date(2026, 7, 1),
            day_count_convention=DayCountConvention.THIRTY_360,
        )
        assert clean == Decimal("98.50")
