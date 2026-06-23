"""Tests for the derivatives daily MTM settlement engine."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType, DeliveryType, ProductSegment
from src.derivatives.mtm_engine import compute_daily_mtm, get_mtm_summary, net_mtm_by_counterparty


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_futures_contract(session, contract_id="NIFTY-FUT-1", lot_size=50, expiry=date(2026, 6, 25)):
    c = DerivativeContract(
        contract_id=contract_id,
        underlying="NIFTY",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.FUTURES,
        option_type=None,
        delivery_type=DeliveryType.CASH,
        strike_price=None,
        lot_size=lot_size,
        expiry_date=expiry,
    )
    session.add(c)
    session.commit()
    return c


def _add_position(session, contract_id, counterparty_id, buy_sell, quantity, trade_price, position_date):
    p = DerivativePosition(
        position_id=str(uuid.uuid4()),
        contract_id=contract_id,
        counterparty_id=counterparty_id,
        buy_sell=buy_sell,
        quantity=quantity,
        trade_price=trade_price,
        position_date=position_date,
    )
    session.add(p)
    session.commit()
    return p


class TestComputeDailyMTM:
    def test_long_position_gain(self, db_session):
        _add_futures_contract(db_session)
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 2, Decimal("22500.00"), date(2026, 6, 1))

        records = compute_daily_mtm(db_session, date(2026, 6, 2), {"NIFTY-FUT-1": Decimal("22600.00")})
        assert len(records) == 1
        # (22600 - 22500) * 2 lots * 50 lot_size = 10000
        assert records[0].mtm_amount == Decimal("10000.00")

    def test_short_position_loss_on_price_rise(self, db_session):
        _add_futures_contract(db_session)
        _add_position(db_session, "NIFTY-FUT-1", "BRK-002", BuySell.SELL, 1, Decimal("22500.00"), date(2026, 6, 1))

        records = compute_daily_mtm(db_session, date(2026, 6, 2), {"NIFTY-FUT-1": Decimal("22600.00")})
        assert records[0].mtm_amount == Decimal("-5000.00")

    def test_second_day_uses_prior_settlement_price(self, db_session):
        _add_futures_contract(db_session)
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 1, Decimal("22500.00"), date(2026, 6, 1))

        compute_daily_mtm(db_session, date(2026, 6, 2), {"NIFTY-FUT-1": Decimal("22600.00")})
        day2 = compute_daily_mtm(db_session, date(2026, 6, 3), {"NIFTY-FUT-1": Decimal("22650.00")})

        # Day 2 P/L computed off day-1 settlement price (22600), not trade price.
        assert day2[0].mtm_amount == Decimal("2500.00")

    def test_no_positions_for_contract_returns_empty(self, db_session):
        _add_futures_contract(db_session)
        records = compute_daily_mtm(db_session, date(2026, 6, 2), {"NIFTY-FUT-1": Decimal("22600.00")})
        assert records == []

    def test_empty_price_dict_returns_empty(self, db_session):
        assert compute_daily_mtm(db_session, date(2026, 6, 2), {}) == []


class TestNetMTMByCounterparty:
    def test_nets_multiple_records(self, db_session):
        _add_futures_contract(db_session)
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 1, Decimal("22500.00"), date(2026, 6, 1))
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.SELL, 1, Decimal("22400.00"), date(2026, 6, 1))

        records = compute_daily_mtm(db_session, date(2026, 6, 2), {"NIFTY-FUT-1": Decimal("22600.00")})
        net = net_mtm_by_counterparty(records)
        # +5000 (long) - 10000 (short) = -5000
        assert net["BRK-001"] == Decimal("-5000.00")


class TestMTMSummary:
    def test_summary_structure(self, db_session):
        _add_futures_contract(db_session)
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 1, Decimal("22500.00"), date(2026, 6, 1))
        records = compute_daily_mtm(db_session, date(2026, 6, 2), {"NIFTY-FUT-1": Decimal("22600.00")})
        summary = get_mtm_summary(records)
        assert summary["total_positions"] == 1
        assert summary["total_pnl"] == "5000.00"

    def test_empty_summary(self):
        summary = get_mtm_summary([])
        assert summary["total_positions"] == 0
