"""Tests for the exposure margin module."""

from decimal import Decimal

from src.margins.exposure_margin import compute_exposure_margin


class TestComputeExposureMargin:
    def test_index_uses_fixed_pct(self):
        margin = compute_exposure_margin(
            underlying_price=Decimal("22500.00"), lot_size=50, net_quantity_lots=2, is_index=True
        )
        # notional = 22500*50*2 = 2,250,000; 3% = 67,500
        assert margin == Decimal("67500.00")

    def test_stock_uses_fixed_pct_when_no_volatility_given(self):
        margin = compute_exposure_margin(
            underlying_price=Decimal("2900.00"), lot_size=505, net_quantity_lots=1, is_index=False
        )
        # notional = 2900*505 = 1,464,500; 5% = 73,225
        assert margin == Decimal("73225.00")

    def test_stock_uses_higher_of_fixed_or_volatility_based(self):
        margin = compute_exposure_margin(
            underlying_price=Decimal("2900.00"), lot_size=505, net_quantity_lots=1, is_index=False,
            std_dev_pct=Decimal("10.0"),
        )
        # vol_pct = 10% * 1.5 = 15% > fixed 5% -> notional 1,464,500 * 15% = 219,675
        assert margin == Decimal("219675.00")

    def test_zero_quantity_yields_zero_margin(self):
        margin = compute_exposure_margin(
            underlying_price=Decimal("22500.00"), lot_size=50, net_quantity_lots=0, is_index=True
        )
        assert margin == Decimal("0")
