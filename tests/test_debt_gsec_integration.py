"""Tests for read-only CCIL G-Sec position reconciliation."""

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DebtTrade
from src.models.enums import DebtTradeStatus, ProductSegment
from src.debt.gsec_integration import (
    derive_gsec_positions,
    get_gsec_recon_summary,
    reconcile_ccil_positions,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_settled_gsec_trade(session, trade_id, buyer, seller, quantity, settlement_date):
    trade = DebtTrade(
        trade_id=trade_id,
        isin="IN0020230012",
        buyer_id=buyer,
        seller_id=seller,
        quantity=quantity,
        clean_price=Decimal("105.25"),
        trade_date=settlement_date,
        settlement_date=settlement_date,
        product_segment=ProductSegment.DEBT_GSEC,
        source="CCIL",
        status=DebtTradeStatus.SETTLED,
    )
    session.add(trade)
    session.commit()


class TestDeriveGsecPositions:
    def test_aggregates_buy_and_sell_legs(self, db_session):
        _add_settled_gsec_trade(db_session, "T1", "BRK-001", "BRK-002", 100, date(2026, 6, 24))
        positions = derive_gsec_positions(db_session, date(2026, 6, 24))
        assert positions[("BRK-001", "IN0020230012")] == 100
        assert positions[("BRK-002", "IN0020230012")] == -100

    def test_excludes_unsettled_trades(self, db_session):
        trade = DebtTrade(
            trade_id="T2",
            isin="IN0020230012",
            buyer_id="BRK-003",
            seller_id="BRK-004",
            quantity=50,
            clean_price=Decimal("100"),
            trade_date=date(2026, 6, 24),
            settlement_date=date(2026, 6, 24),
            product_segment=ProductSegment.DEBT_GSEC,
            source="CCIL",
            status=DebtTradeStatus.PENDING,
        )
        db_session.add(trade)
        db_session.commit()
        positions = derive_gsec_positions(db_session, date(2026, 6, 24))
        assert positions == {}


class TestReconcileCcilPositions:
    def test_matching_positions_reconciled(self, db_session):
        _add_settled_gsec_trade(db_session, "T1", "BRK-001", "BRK-002", 100, date(2026, 6, 24))
        ccil_positions = {("BRK-001", "IN0020230012"): 100, ("BRK-002", "IN0020230012"): -100}
        results = reconcile_ccil_positions(db_session, date(2026, 6, 24), ccil_positions)
        assert all(r.is_reconciled for r in results)

    def test_mismatched_position_flagged(self, db_session):
        _add_settled_gsec_trade(db_session, "T1", "BRK-001", "BRK-002", 100, date(2026, 6, 24))
        ccil_positions = {("BRK-001", "IN0020230012"): 90, ("BRK-002", "IN0020230012"): -100}
        results = reconcile_ccil_positions(db_session, date(2026, 6, 24), ccil_positions)
        mismatch = next(r for r in results if r.counterparty_id == "BRK-001")
        assert mismatch.is_reconciled is False
        assert mismatch.difference == 10

    def test_summary_counts(self, db_session):
        _add_settled_gsec_trade(db_session, "T1", "BRK-001", "BRK-002", 100, date(2026, 6, 24))
        ccil_positions = {("BRK-001", "IN0020230012"): 90, ("BRK-002", "IN0020230012"): -100}
        results = reconcile_ccil_positions(db_session, date(2026, 6, 24), ccil_positions)
        summary = get_gsec_recon_summary(results)
        assert summary["total_positions"] == 2
        assert summary["unreconciled"] == 1
