"""Tests for the options premium settlement engine."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType, DeliveryType, OptionType, ProductSegment
from src.derivatives.premium_engine import compute_premium_obligations, get_premium_summary


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_option_contract(session, contract_id="NIFTY-CE-22500", lot_size=50):
    c = DerivativeContract(
        contract_id=contract_id,
        underlying="NIFTY",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.OPTIONS,
        option_type=OptionType.CALL,
        delivery_type=DeliveryType.CASH,
        strike_price=Decimal("22500.00"),
        lot_size=lot_size,
        expiry_date=date(2026, 6, 25),
    )
    session.add(c)
    session.commit()
    return c


def _add_futures_contract(session, contract_id="NIFTY-FUT-1", lot_size=50):
    c = DerivativeContract(
        contract_id=contract_id,
        underlying="NIFTY",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.FUTURES,
        option_type=None,
        delivery_type=DeliveryType.CASH,
        strike_price=None,
        lot_size=lot_size,
        expiry_date=date(2026, 6, 25),
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


class TestComputePremiumObligations:
    def test_buyer_pays_seller_receives(self, db_session):
        _add_option_contract(db_session)
        _add_position(db_session, "NIFTY-CE-22500", "BRK-001", BuySell.BUY, 1, Decimal("100.00"), date(2026, 6, 1))
        _add_position(db_session, "NIFTY-CE-22500", "BRK-002", BuySell.SELL, 1, Decimal("100.00"), date(2026, 6, 1))

        net = compute_premium_obligations(db_session, date(2026, 6, 1))
        assert net["BRK-001"] == Decimal("-5000.00")
        assert net["BRK-002"] == Decimal("5000.00")

    def test_futures_positions_excluded(self, db_session):
        _add_futures_contract(db_session)
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 1, Decimal("22500.00"), date(2026, 6, 1))

        net = compute_premium_obligations(db_session, date(2026, 6, 1))
        assert net == {}

    def test_only_positions_opened_on_trade_date(self, db_session):
        _add_option_contract(db_session)
        _add_position(db_session, "NIFTY-CE-22500", "BRK-001", BuySell.BUY, 1, Decimal("100.00"), date(2026, 6, 1))

        net = compute_premium_obligations(db_session, date(2026, 6, 2))
        assert net == {}

    def test_no_positions_returns_empty(self, db_session):
        assert compute_premium_obligations(db_session, date(2026, 6, 1)) == {}


class TestPremiumSummary:
    def test_summary_totals(self, db_session):
        _add_option_contract(db_session)
        _add_position(db_session, "NIFTY-CE-22500", "BRK-001", BuySell.BUY, 1, Decimal("100.00"), date(2026, 6, 1))
        _add_position(db_session, "NIFTY-CE-22500", "BRK-002", BuySell.SELL, 1, Decimal("100.00"), date(2026, 6, 1))

        net = compute_premium_obligations(db_session, date(2026, 6, 1))
        summary = get_premium_summary(net)
        assert summary["counterparties"] == 2
        assert summary["total_payable"] == "-5000.00"
        assert summary["total_receivable"] == "5000.00"

    def test_empty_summary(self):
        summary = get_premium_summary({})
        assert summary["counterparties"] == 0
