"""Tests for the SGF default waterfall simulation."""

from decimal import Decimal

from src.sgf.waterfall import WaterfallInputs, get_waterfall_summary, run_default_waterfall


def _inputs(**overrides):
    base = dict(
        defaulter_margin_collateral=Decimal("400000"),
        defaulter_base_capital=Decimal("200000"),
        defaulter_sgf_contribution=Decimal("100000"),
        nse_sgf_contribution=Decimal("150000"),
        other_cm_sgf_contributions={"CM-A": Decimal("200000"), "CM-B": Decimal("100000")},
        nse_other_resources=Decimal("100000"),
        insurance_cover=Decimal("0"),
    )
    base.update(overrides)
    return WaterfallInputs(**base)


class TestRunDefaultWaterfall:
    def test_fully_covered_by_first_layer(self):
        steps = run_default_waterfall(Decimal("300000"), _inputs())
        assert len(steps) == 1
        assert steps[0].applied == Decimal("300000")
        assert steps[0].shortfall_after == Decimal("0")

    def test_cascades_through_multiple_layers(self):
        steps = run_default_waterfall(Decimal("750000"), _inputs())
        assert steps[0].step_name == "Defaulter margins & collateral"
        assert steps[0].applied == Decimal("400000")
        assert steps[1].applied == Decimal("200000")
        assert steps[2].applied == Decimal("100000")
        assert steps[3].applied == Decimal("50000")
        assert steps[-1].shortfall_after == Decimal("0")

    def test_exhausts_all_layers_with_residual_shortfall(self):
        steps = run_default_waterfall(Decimal("10000000"), _inputs())
        total_available = Decimal("400000") + Decimal("200000") + Decimal("100000") + Decimal("150000") + Decimal("300000") + Decimal("100000") + Decimal("0")
        assert steps[-1].shortfall_after == Decimal("10000000") - total_available
        assert len(steps) == 7


class TestGetWaterfallSummary:
    def test_summary_fully_covered(self):
        steps = run_default_waterfall(Decimal("300000"), _inputs())
        summary = get_waterfall_summary(steps)
        assert summary["fully_covered"] is True
        assert summary["total_covered"] == Decimal("300000")

    def test_summary_not_fully_covered(self):
        steps = run_default_waterfall(Decimal("10000000"), _inputs())
        summary = get_waterfall_summary(steps)
        assert summary["fully_covered"] is False
        assert summary["final_shortfall"] > 0
