"""Tests for IRD government bond futures conversion factor and CTD logic."""

from decimal import Decimal

from src.derivatives.bond_futures import (
    DeliverableBond,
    build_delivery_basket,
    compute_conversion_factor,
    compute_delivery_cost,
    identify_cheapest_to_deliver,
)


class TestComputeConversionFactor:
    def test_par_bond_yields_factor_of_one(self):
        cf = compute_conversion_factor(
            coupon_rate_pct=Decimal("6"), years_to_maturity=Decimal("10"), notional_coupon_pct=Decimal("6")
        )
        assert abs(cf - Decimal("1.0000")) < Decimal("0.001")

    def test_high_coupon_bond_has_factor_above_one(self):
        cf = compute_conversion_factor(
            coupon_rate_pct=Decimal("8"), years_to_maturity=Decimal("10"), notional_coupon_pct=Decimal("6")
        )
        assert cf > Decimal("1.0")

    def test_zero_maturity_returns_one(self):
        cf = compute_conversion_factor(
            coupon_rate_pct=Decimal("6"), years_to_maturity=Decimal("0"), notional_coupon_pct=Decimal("6")
        )
        assert cf == Decimal("1.0000")


class TestComputeDeliveryCost:
    def test_cost_is_quoted_minus_invoice(self):
        cost = compute_delivery_cost(
            quoted_price=Decimal("105"), futures_settlement_price=Decimal("100"), conversion_factor=Decimal("1.02")
        )
        assert cost == Decimal("3.0000")


class TestDeliveryBasketAndCtd:
    def test_ctd_is_lowest_delivery_cost(self):
        bonds = [
            DeliverableBond("BOND-A", Decimal("6"), Decimal("10"), Decimal("100")),
            DeliverableBond("BOND-B", Decimal("8"), Decimal("10"), Decimal("118")),
        ]
        ctd = identify_cheapest_to_deliver(bonds, Decimal("100"), Decimal("6"))
        assert ctd["isin"] == "BOND-A"

    def test_basket_sorted_ascending_by_cost(self):
        bonds = [
            DeliverableBond("BOND-A", Decimal("6"), Decimal("10"), Decimal("100")),
            DeliverableBond("BOND-B", Decimal("8"), Decimal("10"), Decimal("118")),
        ]
        basket = build_delivery_basket(bonds, Decimal("100"), Decimal("6"))
        assert basket[0]["delivery_cost"] <= basket[1]["delivery_cost"]

    def test_empty_basket_raises(self):
        try:
            identify_cheapest_to_deliver([], Decimal("100"), Decimal("6"))
            assert False, "expected ValueError"
        except ValueError:
            pass
