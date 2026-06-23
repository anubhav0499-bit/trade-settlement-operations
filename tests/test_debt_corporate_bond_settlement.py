"""Tests for DvP-I gross settlement of corporate bond / debt trades."""

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DebtTrade
from src.models.enums import DebtTradeStatus, ProductSegment
from src.debt.corporate_bond_settlement import (
    check_settlement_failure,
    get_atomic_settlement_summary,
    get_settlement_summary,
    mark_funds_received,
    mark_securities_received,
    settle_dvp1,
    settle_dvp_atomic,
    settle_dvp_atomic_batch,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_trade(session, trade_id="T1", settlement_date=date(2026, 6, 25)):
    trade = DebtTrade(
        trade_id=trade_id,
        isin="INE001A01036",
        buyer_id="BRK-001",
        seller_id="BRK-002",
        quantity=10,
        clean_price=Decimal("98.50"),
        trade_date=date(2026, 6, 23),
        settlement_date=settlement_date,
        product_segment=ProductSegment.DEBT_CORP_BOND,
        source="CBRICS",
        status=DebtTradeStatus.PENDING,
    )
    session.add(trade)
    session.commit()
    return trade


class TestMarkLegsAndSettle:
    def test_both_legs_required_to_settle(self, db_session):
        _add_trade(db_session)
        mark_securities_received(db_session, "T1")
        trade = db_session.query(DebtTrade).filter_by(trade_id="T1").one()
        assert trade.status == DebtTradeStatus.PENDING

        mark_funds_received(db_session, "T1")
        trade = db_session.query(DebtTrade).filter_by(trade_id="T1").one()
        assert trade.status == DebtTradeStatus.SETTLED

    def test_settle_dvp1_noop_if_legs_outstanding(self, db_session):
        _add_trade(db_session)
        trade = settle_dvp1(db_session, "T1")
        assert trade.status == DebtTradeStatus.PENDING


class TestCheckSettlementFailure:
    def test_fails_when_overdue_with_outstanding_leg(self, db_session):
        trade = _add_trade(db_session, settlement_date=date(2026, 6, 1))
        mark_securities_received(db_session, "T1")
        failed = check_settlement_failure(trade, current_date=date(2026, 6, 23))
        assert failed is True
        assert trade.status == DebtTradeStatus.FAILED

    def test_not_failed_when_within_settlement_date(self, db_session):
        trade = _add_trade(db_session, settlement_date=date(2026, 6, 25))
        failed = check_settlement_failure(trade, current_date=date(2026, 6, 23))
        assert failed is False
        assert trade.status == DebtTradeStatus.PENDING

    def test_settled_trade_never_fails(self, db_session):
        _add_trade(db_session, settlement_date=date(2026, 6, 1))
        mark_securities_received(db_session, "T1")
        mark_funds_received(db_session, "T1")
        trade = db_session.query(DebtTrade).filter_by(trade_id="T1").one()
        failed = check_settlement_failure(trade, current_date=date(2026, 6, 23))
        assert failed is False


class TestSettleDvpAtomic:
    def test_both_legs_available_settles_immediately(self, db_session):
        _add_trade(db_session)
        trade = settle_dvp_atomic(db_session, "T1", securities_available=True, funds_available=True)
        assert trade.status == DebtTradeStatus.SETTLED
        assert trade.securities_received is True
        assert trade.funds_received is True

    def test_only_securities_available_leaves_trade_untouched(self, db_session):
        """The defining contrast with the async path: one leg being ready is
        not enough — atomic settlement sets NEITHER flag, not just one."""
        _add_trade(db_session)
        trade = settle_dvp_atomic(db_session, "T1", securities_available=True, funds_available=False)
        assert trade.status == DebtTradeStatus.PENDING
        assert trade.securities_received is False
        assert trade.funds_received is False

    def test_only_funds_available_leaves_trade_untouched(self, db_session):
        _add_trade(db_session)
        trade = settle_dvp_atomic(db_session, "T1", securities_available=False, funds_available=True)
        assert trade.status == DebtTradeStatus.PENDING
        assert trade.securities_received is False
        assert trade.funds_received is False

    def test_neither_leg_available_leaves_trade_untouched(self, db_session):
        _add_trade(db_session)
        trade = settle_dvp_atomic(db_session, "T1", securities_available=False, funds_available=False)
        assert trade.status == DebtTradeStatus.PENDING

    def test_contrasts_with_async_partial_state(self, db_session):
        """The async path CAN leave a trade half-cleared; atomic never does."""
        _add_trade(db_session, trade_id="ASYNC")
        mark_securities_received(db_session, "ASYNC")
        async_trade = db_session.query(DebtTrade).filter_by(trade_id="ASYNC").one()
        assert async_trade.securities_received is True
        assert async_trade.funds_received is False  # partial state, persisted

        _add_trade(db_session, trade_id="ATOMIC")
        settle_dvp_atomic(db_session, "ATOMIC", securities_available=True, funds_available=False)
        atomic_trade = db_session.query(DebtTrade).filter_by(trade_id="ATOMIC").one()
        assert atomic_trade.securities_received is False  # no partial state
        assert atomic_trade.funds_received is False


class TestSettleDvpAtomicBatch:
    def test_settles_only_trades_with_both_legs_available(self, db_session):
        _add_trade(db_session, trade_id="T1")
        _add_trade(db_session, trade_id="T2")
        _add_trade(db_session, trade_id="T3")

        trades = settle_dvp_atomic_batch(
            db_session, ["T1", "T2", "T3"],
            securities_availability={"T1": True, "T2": True, "T3": False},
            funds_availability={"T1": True, "T2": False, "T3": True},
        )
        statuses = {t.trade_id: t.status for t in trades}
        assert statuses["T1"] == DebtTradeStatus.SETTLED
        assert statuses["T2"] == DebtTradeStatus.PENDING
        assert statuses["T3"] == DebtTradeStatus.PENDING

    def test_trade_missing_from_availability_dicts_treated_as_unavailable(self, db_session):
        _add_trade(db_session, trade_id="T1")
        trades = settle_dvp_atomic_batch(
            db_session, ["T1"], securities_availability={}, funds_availability={},
        )
        assert trades[0].status == DebtTradeStatus.PENDING


class TestGetAtomicSettlementSummary:
    def test_counts_settled_and_unsettled(self, db_session):
        _add_trade(db_session, trade_id="T1")
        _add_trade(db_session, trade_id="T2")
        trades = settle_dvp_atomic_batch(
            db_session, ["T1", "T2"],
            securities_availability={"T1": True, "T2": True},
            funds_availability={"T1": True, "T2": False},
        )
        summary = get_atomic_settlement_summary(trades)
        assert summary == {"total": 2, "settled": 1, "unsettled": 1}

    def test_empty_list_summary(self):
        assert get_atomic_settlement_summary([]) == {"total": 0, "settled": 0, "unsettled": 0}


class TestGetSettlementSummary:
    def test_summary_counts_by_status(self, db_session):
        _add_trade(db_session, trade_id="T1", settlement_date=date(2026, 6, 25))
        _add_trade(db_session, trade_id="T2", settlement_date=date(2026, 6, 25))
        mark_securities_received(db_session, "T2")
        mark_funds_received(db_session, "T2")

        summary = get_settlement_summary(db_session, as_of_date=date(2026, 6, 25))
        assert summary["PENDING"] == 1
        assert summary["SETTLED"] == 1
        assert summary["FAILED"] == 0
