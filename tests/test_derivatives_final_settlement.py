"""Tests for expiry-day final settlement (cash-settled contracts only)."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType, DeliveryType, ProductSegment
from src.derivatives.final_settlement import run_final_settlement


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_contract(session, contract_id, delivery_type, expiry, lot_size=50):
    c = DerivativeContract(
        contract_id=contract_id,
        underlying="NIFTY" if delivery_type == DeliveryType.CASH else "RELIANCE",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.FUTURES,
        option_type=None,
        delivery_type=delivery_type,
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


class TestRunFinalSettlement:
    def test_cash_settled_contract_settles_at_fsp(self, db_session):
        _add_contract(db_session, "NIFTY-FUT-1", DeliveryType.CASH, date(2026, 6, 25))
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 1, Decimal("22500.00"), date(2026, 6, 1))

        records = run_final_settlement(db_session, date(2026, 6, 25), {"NIFTY-FUT-1": Decimal("22650.00")})
        assert len(records) == 1
        assert records[0].mtm_amount == Decimal("7500.00")

    def test_physically_settled_contract_excluded(self, db_session):
        _add_contract(db_session, "RELIANCE-FUT-1", DeliveryType.PHYSICAL, date(2026, 6, 25))
        _add_position(db_session, "RELIANCE-FUT-1", "BRK-001", BuySell.BUY, 1, Decimal("2900.00"), date(2026, 6, 1))

        records = run_final_settlement(db_session, date(2026, 6, 25), {"RELIANCE-FUT-1": Decimal("2950.00")})
        assert records == []

    def test_contract_with_different_expiry_excluded(self, db_session):
        _add_contract(db_session, "NIFTY-FUT-1", DeliveryType.CASH, date(2026, 7, 30))
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 1, Decimal("22500.00"), date(2026, 6, 1))

        records = run_final_settlement(db_session, date(2026, 6, 25), {"NIFTY-FUT-1": Decimal("22650.00")})
        assert records == []

    def test_empty_fsp_dict_returns_empty(self, db_session):
        assert run_final_settlement(db_session, date(2026, 6, 25), {}) == []
