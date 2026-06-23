"""Tests for portfolio stress testing and top-N CM ranking."""

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType, DeliveryType, ProductSegment
from src.risk.stress_test import (
    compute_portfolio_stress_loss,
    compute_position_stress_loss,
    get_contagion_summary,
    get_stress_summary,
    identify_contagion_clusters,
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


def _add_contract(session, contract_id, underlying):
    session.add(DerivativeContract(
        contract_id=contract_id, underlying=underlying, product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.FUTURES, option_type=None, delivery_type=DeliveryType.CASH,
        strike_price=None, lot_size=1, expiry_date=date(2026, 12, 31),
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


class TestIdentifyContagionClusters:
    def test_two_cms_same_underlying_form_a_cluster(self, db_session):
        d = date(2026, 6, 25)
        _add_contract(db_session, "NIFTY-FUT", "NIFTY")
        _add_position(db_session, "P1", "NIFTY-FUT", "CM-1", BuySell.BUY, 100, Decimal("24000"), d)
        _add_position(db_session, "P2", "NIFTY-FUT", "CM-2", BuySell.BUY, 50, Decimal("24000"), d)

        clusters = identify_contagion_clusters(
            db_session, ["CM-1", "CM-2"], d, Decimal("10"), {"NIFTY-FUT": Decimal("24000")}, min_cms=2,
        )
        assert len(clusters) == 1
        assert clusters[0].underlying == "NIFTY"
        assert set(clusters[0].affected_cm_ids) == {"CM-1", "CM-2"}
        assert clusters[0].share_of_total_stress_loss_pct == Decimal("100.00")

    def test_single_cm_on_an_underlying_is_not_a_cluster(self, db_session):
        """Only one CM exposed to this underlying — no contagion, just
        ordinary single-counterparty stress (already covered by
        rank_top_n_stressed_cms)."""
        d = date(2026, 6, 25)
        _add_contract(db_session, "NIFTY-FUT", "NIFTY")
        _add_position(db_session, "P1", "NIFTY-FUT", "CM-1", BuySell.BUY, 100, Decimal("24000"), d)

        clusters = identify_contagion_clusters(
            db_session, ["CM-1"], d, Decimal("10"), {"NIFTY-FUT": Decimal("24000")}, min_cms=2,
        )
        assert clusters == []

    def test_same_cm_two_contracts_same_underlying_counted_once(self, db_session):
        """A CM holding both a future and an option on NIFTY is one
        affected CM for the NIFTY cluster, not two."""
        d = date(2026, 6, 25)
        _add_contract(db_session, "NIFTY-FUT", "NIFTY")
        _add_contract(db_session, "NIFTY-CE", "NIFTY")
        _add_position(db_session, "P1", "NIFTY-FUT", "CM-1", BuySell.BUY, 100, Decimal("24000"), d)
        _add_position(db_session, "P2", "NIFTY-CE", "CM-1", BuySell.BUY, 50, Decimal("180"), d)
        _add_position(db_session, "P3", "NIFTY-FUT", "CM-2", BuySell.BUY, 50, Decimal("24000"), d)

        clusters = identify_contagion_clusters(
            db_session, ["CM-1", "CM-2"], d, Decimal("10"),
            {"NIFTY-FUT": Decimal("24000"), "NIFTY-CE": Decimal("180")}, min_cms=2,
        )
        assert len(clusters) == 1
        assert set(clusters[0].affected_cm_ids) == {"CM-1", "CM-2"}

    def test_different_underlyings_dont_merge_into_one_cluster(self, db_session):
        d = date(2026, 6, 25)
        _add_contract(db_session, "NIFTY-FUT", "NIFTY")
        _add_contract(db_session, "RELIANCE-FUT", "RELIANCE")
        _add_position(db_session, "P1", "NIFTY-FUT", "CM-1", BuySell.BUY, 100, Decimal("24000"), d)
        _add_position(db_session, "P2", "NIFTY-FUT", "CM-2", BuySell.BUY, 100, Decimal("24000"), d)
        _add_position(db_session, "P3", "RELIANCE-FUT", "CM-3", BuySell.BUY, 100, Decimal("1400"), d)
        _add_position(db_session, "P4", "RELIANCE-FUT", "CM-4", BuySell.BUY, 100, Decimal("1400"), d)

        clusters = identify_contagion_clusters(
            db_session, ["CM-1", "CM-2", "CM-3", "CM-4"], d, Decimal("10"),
            {"NIFTY-FUT": Decimal("24000"), "RELIANCE-FUT": Decimal("1400")}, min_cms=2,
        )
        assert {c.underlying for c in clusters} == {"NIFTY", "RELIANCE"}
        assert all(len(c.affected_cm_ids) == 2 for c in clusters)

    def test_clusters_sorted_by_loss_descending(self, db_session):
        d = date(2026, 6, 25)
        _add_contract(db_session, "NIFTY-FUT", "NIFTY")
        _add_contract(db_session, "RELIANCE-FUT", "RELIANCE")
        _add_position(db_session, "P1", "NIFTY-FUT", "CM-1", BuySell.BUY, 1000, Decimal("24000"), d)
        _add_position(db_session, "P2", "NIFTY-FUT", "CM-2", BuySell.BUY, 1000, Decimal("24000"), d)
        _add_position(db_session, "P3", "RELIANCE-FUT", "CM-3", BuySell.BUY, 10, Decimal("1400"), d)
        _add_position(db_session, "P4", "RELIANCE-FUT", "CM-4", BuySell.BUY, 10, Decimal("1400"), d)

        clusters = identify_contagion_clusters(
            db_session, ["CM-1", "CM-2", "CM-3", "CM-4"], d, Decimal("10"),
            {"NIFTY-FUT": Decimal("24000"), "RELIANCE-FUT": Decimal("1400")}, min_cms=2,
        )
        assert clusters[0].underlying == "NIFTY"
        assert clusters[0].total_stress_loss > clusters[1].total_stress_loss

    def test_no_positions_yields_no_clusters(self, db_session):
        d = date(2026, 6, 25)
        clusters = identify_contagion_clusters(db_session, ["CM-1"], d, Decimal("10"), {}, min_cms=2)
        assert clusters == []


class TestGetContagionSummary:
    def test_summarizes_largest_cluster(self, db_session):
        d = date(2026, 6, 25)
        _add_contract(db_session, "NIFTY-FUT", "NIFTY")
        _add_position(db_session, "P1", "NIFTY-FUT", "CM-1", BuySell.BUY, 100, Decimal("24000"), d)
        _add_position(db_session, "P2", "NIFTY-FUT", "CM-2", BuySell.BUY, 100, Decimal("24000"), d)

        clusters = identify_contagion_clusters(
            db_session, ["CM-1", "CM-2"], d, Decimal("10"), {"NIFTY-FUT": Decimal("24000")}, min_cms=2,
        )
        summary = get_contagion_summary(clusters)
        assert summary["cluster_count"] == 1
        assert summary["largest_cluster_underlying"] == "NIFTY"
        assert summary["largest_cluster_cm_count"] == 2

    def test_empty_clusters_summary(self):
        summary = get_contagion_summary([])
        assert summary["cluster_count"] == 0
        assert summary["largest_cluster_underlying"] is None
