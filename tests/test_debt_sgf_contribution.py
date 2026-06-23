"""Tests for the SGF issuer contribution calculation."""

from datetime import date
from decimal import Decimal

from src.debt.sgf_contribution import compute_sgf_issuer_contribution


class TestComputeSgfIssuerContribution:
    def test_five_year_tenor(self):
        contribution = compute_sgf_issuer_contribution(
            issuance_value=Decimal("100000000"),
            issue_date=date(2026, 1, 1),
            maturity_date=date(2031, 1, 1),
        )
        # ~5 years * 0.5bps (0.00005) * 100,000,000 = ~25,000
        assert Decimal("24900") < contribution < Decimal("25100")

    def test_zero_tenor_yields_zero(self):
        contribution = compute_sgf_issuer_contribution(
            issuance_value=Decimal("100000000"),
            issue_date=date(2026, 1, 1),
            maturity_date=date(2026, 1, 1),
        )
        assert contribution == Decimal("0.00")
