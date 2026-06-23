"""Tests for the options exercise & assignment engine."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType, DeliveryType, OptionType, ProductSegment
from src.derivatives.exercise_engine import (
    assign_short_positions,
    exercise_long_positions,
    is_in_the_money,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_option_contract(session, option_type, strike=Decimal("22500.00"), lot_size=50):
    c = DerivativeContract(
        contract_id="NIFTY-OPT-1",
        underlying="NIFTY",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.OPTIONS,
        option_type=option_type,
        delivery_type=DeliveryType.CASH,
        strike_price=strike,
        lot_size=lot_size,
        expiry_date=date(2026, 6, 25),
    )
    session.add(c)
    session.commit()
    return c


def _add_position(session, contract_id, counterparty_id, buy_sell, quantity, position_id=None):
    p = DerivativePosition(
        position_id=position_id or str(uuid.uuid4()),
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


class TestIsInTheMoney:
    def test_call_itm_when_fsp_above_strike(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL, strike=Decimal("22500.00"))
        assert is_in_the_money(c, Decimal("22600.00")) is True

    def test_call_otm_when_fsp_below_strike(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL, strike=Decimal("22500.00"))
        assert is_in_the_money(c, Decimal("22400.00")) is False

    def test_put_itm_when_fsp_below_strike(self, db_session):
        c = _add_option_contract(db_session, OptionType.PUT, strike=Decimal("22500.00"))
        assert is_in_the_money(c, Decimal("22400.00")) is True

    def test_put_otm_when_fsp_above_strike(self, db_session):
        c = _add_option_contract(db_session, OptionType.PUT, strike=Decimal("22500.00"))
        assert is_in_the_money(c, Decimal("22600.00")) is False


class TestExerciseLongPositions:
    def test_itm_position_auto_exercised(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-001", BuySell.BUY, 2, position_id="POS-1")

        results = exercise_long_positions(db_session, c, Decimal("22600.00"))
        assert results[0].exercised_quantity == 2
        assert results[0].is_itm is True

    def test_otm_position_not_exercised(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-001", BuySell.BUY, 2, position_id="POS-1")

        results = exercise_long_positions(db_session, c, Decimal("22400.00"))
        assert results[0].exercised_quantity == 0
        assert results[0].is_itm is False

    def test_dne_opt_out_blocks_exercise(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-001", BuySell.BUY, 2, position_id="POS-1")

        results = exercise_long_positions(
            db_session, c, Decimal("22600.00"), dne_position_ids={"POS-1"}
        )
        assert results[0].exercised_quantity == 0
        assert results[0].is_itm is True

    def test_short_positions_excluded(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-002", BuySell.SELL, 2, position_id="POS-2")

        results = exercise_long_positions(db_session, c, Decimal("22600.00"))
        assert results == []


class TestAssignShortPositions:
    def test_no_exercise_means_no_assignment(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-002", BuySell.SELL, 2, position_id="POS-2")
        exercise_results = exercise_long_positions(db_session, c, Decimal("22400.00"))

        assignments = assign_short_positions(db_session, c, exercise_results, seed=1)
        assert assignments == []

    def test_full_assignment_when_one_short(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-001", BuySell.BUY, 2, position_id="POS-1")
        _add_position(db_session, c.contract_id, "BRK-002", BuySell.SELL, 2, position_id="POS-2")

        exercise_results = exercise_long_positions(db_session, c, Decimal("22600.00"))
        assignments = assign_short_positions(db_session, c, exercise_results, seed=1)

        assert len(assignments) == 1
        assert assignments[0].counterparty_id == "BRK-002"
        assert assignments[0].assigned_quantity == 2

    def test_assignment_is_deterministic_for_same_seed(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-001", BuySell.BUY, 3, position_id="POS-1")
        _add_position(db_session, c.contract_id, "BRK-002", BuySell.SELL, 2, position_id="POS-2")
        _add_position(db_session, c.contract_id, "BRK-003", BuySell.SELL, 2, position_id="POS-3")

        exercise_results = exercise_long_positions(db_session, c, Decimal("22600.00"))
        a1 = assign_short_positions(db_session, c, exercise_results, seed=42)
        a2 = assign_short_positions(db_session, c, exercise_results, seed=42)

        key1 = sorted((a.position_id, a.assigned_quantity) for a in a1)
        key2 = sorted((a.position_id, a.assigned_quantity) for a in a2)
        assert key1 == key2

        total_assigned = sum(a.assigned_quantity for a in a1)
        assert total_assigned == 3

    def test_no_shorts_returns_empty(self, db_session):
        c = _add_option_contract(db_session, OptionType.CALL)
        _add_position(db_session, c.contract_id, "BRK-001", BuySell.BUY, 2, position_id="POS-1")

        exercise_results = exercise_long_positions(db_session, c, Decimal("22600.00"))
        assignments = assign_short_positions(db_session, c, exercise_results, seed=1)
        assert assignments == []
