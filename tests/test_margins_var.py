"""Tests for the EWMA volatility / VaR margin model."""

from decimal import Decimal

from src.margins.var_model import compute_var_margin, ewma_volatility


class TestEwmaVolatility:
    def test_empty_returns_zero(self):
        assert ewma_volatility([]) == Decimal("0")

    def test_single_return_is_its_own_volatility(self):
        vol = ewma_volatility([Decimal("0.02")])
        assert vol == Decimal("0.02")

    def test_constant_returns_converge_to_that_magnitude(self):
        returns = [Decimal("0.01")] * 50
        vol = ewma_volatility(returns)
        assert abs(vol - Decimal("0.01")) < Decimal("0.0001")

    def test_custom_lambda_accepted(self):
        vol = ewma_volatility([Decimal("0.01"), Decimal("0.02")], lambda_=Decimal("0.5"))
        assert vol > Decimal("0")


class TestComputeVarMargin:
    def test_margin_scales_with_price_and_volatility(self):
        margin = compute_var_margin(
            price=Decimal("1000.00"), volatility=Decimal("0.02"), confidence_z=Decimal("2.33")
        )
        assert margin == Decimal("46.60")

    def test_uses_configured_default_z_when_not_supplied(self):
        margin = compute_var_margin(price=Decimal("1000.00"), volatility=Decimal("0.02"))
        assert margin == Decimal("46.60")

    def test_zero_volatility_yields_zero_margin(self):
        margin = compute_var_margin(price=Decimal("1000.00"), volatility=Decimal("0"))
        assert margin == Decimal("0.00")
