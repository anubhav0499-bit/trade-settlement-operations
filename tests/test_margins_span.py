"""Tests for the SPAN-style portfolio margin engine."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType, DeliveryType, OptionType, ProductSegment
from src.margins.span_engine import compute_span_margin


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_contract(session, contract_id, contract_type, expiry, option_type=None, strike=None, lot_size=50):
    c = DerivativeContract(
        contract_id=contract_id,
        underlying="NIFTY",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=contract_type,
        option_type=option_type,
        delivery_type=DeliveryType.CASH,
        strike_price=strike,
        lot_size=lot_size,
        expiry_date=expiry,
    )
    session.add(c)
    session.commit()
    return c


def _add_position(session, contract_id, counterparty_id, buy_sell, quantity):
    p = DerivativePosition(
        position_id=str(uuid.uuid4()),
        contract_id=contract_id,
        counterparty_id=counterparty_id,
        buy_sell=buy_sell,
        quantity=quantity,
        trade_price=Decimal("100.00"),
        position_date=date(2026, 6, 1),
    )
    session.add(p)
    session.commit()
    return p


class TestComputeSpanMarginFutures:
    def test_long_futures_scenario_margin(self, db_session):
        _add_contract(db_session, "NIFTY-FUT-1", ContractType.FUTURES, date(2026, 6, 25))
        _add_position(db_session, "NIFTY-FUT-1", "BRK-001", BuySell.BUY, 2)

        result = compute_span_margin(
            db_session, "BRK-001", "NIFTY", Decimal("22500.00"), is_index=True
        )
        assert result.scenario_margin == Decimal("202500.00")
        assert result.total_margin == Decimal("202500.00")
        assert result.short_option_minimum == Decimal("0.00")
        assert result.calendar_spread_charge == Decimal("0.00")

    def test_short_futures_scenario_margin(self, db_session):
        _add_contract(db_session, "NIFTY-FUT-1", ContractType.FUTURES, date(2026, 6, 25))
        _add_position(db_session, "NIFTY-FUT-1", "BRK-002", BuySell.SELL, 2)

        result = compute_span_margin(
            db_session, "BRK-002", "NIFTY", Decimal("22500.00"), is_index=True
        )
        assert result.scenario_margin == Decimal("202500.00")

    def test_calendar_spread_charge_applied_across_expiries(self, db_session):
        _add_contract(db_session, "NIFTY-FUT-NEAR", ContractType.FUTURES, date(2026, 6, 25))
        _add_contract(db_session, "NIFTY-FUT-FAR", ContractType.FUTURES, date(2026, 7, 30))
        _add_position(db_session, "NIFTY-FUT-NEAR", "BRK-001", BuySell.BUY, 3)
        _add_position(db_session, "NIFTY-FUT-FAR", "BRK-001", BuySell.SELL, 2)

        result = compute_span_margin(
            db_session, "BRK-001", "NIFTY", Decimal("22500.00"), is_index=True
        )
        assert result.calendar_spread_charge == Decimal("400.00")
        assert result.scenario_margin == Decimal("101250.00")
        assert result.total_margin == Decimal("101650.00")


class TestComputeSpanMarginOptions:
    def test_short_option_minimum_floor(self, db_session):
        _add_contract(
            db_session, "NIFTY-CE-22500", ContractType.OPTIONS, date(2026, 6, 25),
            option_type=OptionType.CALL, strike=Decimal("22500.00"),
        )
        _add_position(db_session, "NIFTY-CE-22500", "BRK-001", BuySell.SELL, 2)

        result = compute_span_margin(
            db_session, "BRK-001", "NIFTY", Decimal("22500.00"), is_index=True,
            delta_by_contract={"NIFTY-CE-22500": Decimal("0.5")},
        )
        assert result.short_option_minimum == Decimal("67500.00")
        assert result.scenario_margin == Decimal("101250.00")
        assert result.total_margin == Decimal("101250.00")

    def test_net_option_value_credits_long_premium(self, db_session):
        _add_contract(
            db_session, "NIFTY-CE-22500", ContractType.OPTIONS, date(2026, 6, 25),
            option_type=OptionType.CALL, strike=Decimal("22500.00"),
        )
        _add_position(db_session, "NIFTY-CE-22500", "BRK-001", BuySell.BUY, 2)

        result = compute_span_margin(
            db_session, "BRK-001", "NIFTY", Decimal("22500.00"), is_index=True,
            delta_by_contract={"NIFTY-CE-22500": Decimal("0.5")},
            option_value_by_contract={"NIFTY-CE-22500": Decimal("120.00")},
        )
        assert result.net_option_value == Decimal("12000.00")
        assert result.scenario_margin == Decimal("101250.00")
        assert result.total_margin == Decimal("89250.00")

    def test_no_positions_yields_zero_margin(self, db_session):
        _add_contract(
            db_session, "NIFTY-CE-22500", ContractType.OPTIONS, date(2026, 6, 25),
            option_type=OptionType.CALL, strike=Decimal("22500.00"),
        )
        result = compute_span_margin(db_session, "BRK-999", "NIFTY", Decimal("22500.00"), is_index=True)
        assert result.total_margin == Decimal("0.00")
