"""Tests for portfolio stress testing and top-N CM ranking."""

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativePosition
from src.models.enums import BuySell
from src.risk.stress_test import (
    compute_portfolio_stress_loss,
    compute_position_stress_loss,
    get_stress_summary,
    rank_top_n_stressed_cms,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_position(session, position_id, contract_id, cp_id, buy_sell, qty, price, pos_date):
    session.add(DerivativePosition(
        position_id=position_id,
        contract_id=contract_id,
        counterparty_id=cp_id,
        buy_sell=buy_sell,
        quantity=qty,
        trade_price=price,
        position_date=pos_date,
    ))
    session.commit()


class TestComputePositionStressLoss:
    def test_long_position_loses_on_price_drop(self):
        pos = DerivativePosition(
            position_id="P1", contract_id="C1", counterparty_id="CM-1",
            buy_sell=BuySell.BUY, quantity=100, trade_price=Decimal("100"),
            position_date=date(2026, 6, 25),
        )
        loss = compute_position_stress_loss(pos, Decimal("100"), Decimal("10"))
        assert loss == Decimal("1000.00")

    def test_short_position_loses_on_price_rise(self):
        pos = DerivativePosition(
            position_id="P2", contract_id="C1", counterparty_id="CM-1",
            buy_sell=BuySell.SELL, quantity=100, trade_price=Decimal("100"),
            position_date=date(2026, 6, 25),
        )
        loss = compute_position_stress_loss(pos, Decimal("100"), Decimal("10"))
        assert loss == Decimal("1000.00")


class TestComputePortfolioStressLoss:
    def test_aggregates_multiple_positions(self, db_session):
        d = date(2026, 6, 25)
        _add_position(db_session, "P1", "C1", "CM-1", BuySell.BUY, 100, Decimal("100"), d)
        _add_position(db_session, "P2", "C2", "CM-1", BuySell.SELL, 50, Decimal("200"), d)

        prices = {"C1": Decimal("100"), "C2": Decimal("200")}
        loss = compute_portfolio_stress_loss(db_session, "CM-1", d, Decimal("10"), prices)
        assert loss == Decimal("1000.00") + Decimal("1000.00")

    def test_missing_reference_price_skipped(self, db_session):
        d = date(2026, 6, 25)
        _add_position(db_session, "P1", "C1", "CM-1", BuySell.BUY, 100, Decimal("100"), d)
        loss = compute_portfolio_stress_loss(db_session, "CM-1", d, Decimal("10"), {})
        assert loss == Decimal("0.00")


class TestRankTopNStressedCms:
    def test_ranks_by_shortfall_descending(self, db_session):
        d = date(2026, 6, 25)
        _add_position(db_session, "P1", "C1", "CM-1", BuySell.BUY, 1000, Decimal("100"), d)
        _add_position(db_session, "P2", "C1", "CM-2", BuySell.BUY, 100, Decimal("100"), d)

        prices = {"C1": Decimal("100")}
        margin_held = {"CM-1": Decimal("5000"), "CM-2": Decimal("5000")}
        results = rank_top_n_stressed_cms(
            db_session, ["CM-1", "CM-2"], d, Decimal("10"), prices, margin_held, top_n=2
        )
        assert results[0].counterparty_id == "CM-1"
        assert results[0].shortfall > results[1].shortfall

    def test_top_n_limits_results(self, db_session):
        d = date(2026, 6, 25)
        for i in range(5):
            _add_position(db_session, f"P{i}", "C1", f"CM-{i}", BuySell.BUY, 100, Decimal("100"), d)
        prices = {"C1": Decimal("100")}
        results = rank_top_n_stressed_cms(
            db_session, [f"CM-{i}" for i in range(5)], d, Decimal("10"), prices, {}, top_n=2
        )
        assert len(results) == 2


class TestGetStressSummary:
    def test_summary_counts_shortfalls(self, db_session):
        d = date(2026, 6, 25)
        _add_position(db_session, "P1", "C1", "CM-1", BuySell.BUY, 1000, Decimal("100"), d)
        prices = {"C1": Decimal("100")}
        results = rank_top_n_stressed_cms(
            db_session, ["CM-1"], d, Decimal("10"), prices, {"CM-1": Decimal("0")}, top_n=1
        )
        summary = get_stress_summary(results)
        assert summary["cms_with_shortfall"] == 1

    def test_empty_results_summary(self):
        summary = get_stress_summary([])
        assert summary["total_stress_loss"] == Decimal("0")
