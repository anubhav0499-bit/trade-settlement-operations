"""Tests for the T+0 parallel settlement path (equity cash)."""

import pytest
from datetime import date, time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Trade
from src.models.enums import (
    BuySell,
    CounterpartyType,
    Exchange,
    ProductSegment,
    SettlementCycle,
    SourceSystem,
)
from src.settlement.t0_engine import (
    compute_t0_obligations,
    get_t0_summary,
    is_trade_eligible_for_t0,
    is_within_obligation_cutoff,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_trade(session, trade_id, isin, qty, price, buy_sell, cp_id, cycle, settle_date):
    session.add(Trade(
        trade_id=trade_id,
        isin=isin,
        security_name="TestCo",
        quantity=qty,
        price=price,
        trade_date=settle_date,
        settlement_date=settle_date,
        settlement_cycle=cycle,
        counterparty_id=cp_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        buy_sell=buy_sell,
        source_system=SourceSystem.OMS,
        product_segment=ProductSegment.EQUITY_CASH,
    ))
    session.commit()


class TestCutoffChecks:
    def test_trade_before_cutoff_eligible(self):
        assert is_trade_eligible_for_t0(time(13, 0)) is True

    def test_trade_after_cutoff_ineligible(self):
        assert is_trade_eligible_for_t0(time(14, 0)) is False

    def test_obligation_within_cutoff(self):
        assert is_within_obligation_cutoff(time(14, 0)) is True

    def test_obligation_past_cutoff(self):
        assert is_within_obligation_cutoff(time(15, 0)) is False


class TestComputeT0Obligations:
    def test_nets_t0_trades_only(self, db_session):
        d = date(2026, 6, 25)
        _add_trade(db_session, "T1", "INE001A01036", 100, 50, BuySell.BUY, "BRK-001", SettlementCycle.T0, d)
        _add_trade(db_session, "T2", "INE001A01036", 40, 50, BuySell.SELL, "BRK-001", SettlementCycle.T0, d)
        _add_trade(db_session, "T3", "INE001A01036", 100, 50, BuySell.BUY, "BRK-002", SettlementCycle.T1, d)

        obligations = compute_t0_obligations(db_session)
        assert len(obligations) == 1
        assert obligations[0].net_quantity == 60
        assert obligations[0].settlement_cycle == SettlementCycle.T0

    def test_no_t0_trades_yields_no_obligations(self, db_session):
        d = date(2026, 6, 25)
        _add_trade(db_session, "T1", "INE001A01036", 100, 50, BuySell.BUY, "BRK-001", SettlementCycle.T1, d)
        assert compute_t0_obligations(db_session) == []


class TestGetT0Summary:
    def test_summary_totals_value(self, db_session):
        d = date(2026, 6, 25)
        _add_trade(db_session, "T1", "INE001A01036", 100, 50, BuySell.BUY, "BRK-001", SettlementCycle.T0, d)
        obligations = compute_t0_obligations(db_session)
        summary = get_t0_summary(obligations)
        assert summary["total"] == 1

    def test_empty_summary(self):
        assert get_t0_summary([])["total"] == 0
