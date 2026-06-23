"""Tests for corporate action cash-flow computations."""

from decimal import Decimal

from src.debt.corporate_actions import (
    compute_call_put_amount,
    compute_coupon_payment,
    compute_redemption_amount,
)


class TestComputeCouponPayment:
    def test_semi_annual_coupon(self):
        amount = compute_coupon_payment(
            face_value=Decimal("100000"),
            coupon_rate_pct=Decimal("8"),
            coupon_frequency=2,
            quantity=10,
        )
        # per unit = 100000 * 8% / 2 = 4000; * 10 units = 40000
        assert amount == Decimal("40000.00")


class TestComputeRedemptionAmount:
    def test_redemption_at_par(self):
        amount = compute_redemption_amount(face_value=Decimal("100000"), quantity=5)
        assert amount == Decimal("500000.00")


class TestComputeCallPutAmount:
    def test_exercise_amount(self):
        amount = compute_call_put_amount(quantity=10, exercise_price=Decimal("101.50"))
        assert amount == Decimal("1015.00")
