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
    get_settlement_summary,
    mark_funds_received,
    mark_securities_received,
    settle_dvp1,
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
