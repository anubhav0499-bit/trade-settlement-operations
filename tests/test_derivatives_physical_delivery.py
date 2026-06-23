"""Tests for stock F&O physical delivery obligation generation and delivery margin."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativeContract, DerivativePosition
from src.models.enums import (
    BuySell,
    ContractType,
    DeliveryType,
    NetDirection,
    ObligationStage,
    ProductSegment,
)
from src.derivatives.exercise_engine import AssignmentResult, ExerciseResult
from src.derivatives.physical_delivery import (
    compute_delivery_margin,
    generate_futures_delivery_obligations,
    generate_option_delivery_obligations,
    get_delivery_summary,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_futures_contract(session, lot_size=505):
    c = DerivativeContract(
        contract_id="RELIANCE-FUT-1",
        underlying="RELIANCE",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.FUTURES,
        option_type=None,
        delivery_type=DeliveryType.PHYSICAL,
        strike_price=None,
        lot_size=lot_size,
        expiry_date=date(2026, 6, 25),
    )
    session.add(c)
    session.commit()
    return c


def _add_option_contract(session, lot_size=505, strike=Decimal("2900.00")):
    c = DerivativeContract(
        contract_id="RELIANCE-OPT-1",
        underlying="RELIANCE",
        product_segment=ProductSegment.EQUITY_FO,
        contract_type=ContractType.OPTIONS,
        option_type=None,
        delivery_type=DeliveryType.PHYSICAL,
        strike_price=strike,
        lot_size=lot_size,
        expiry_date=date(2026, 6, 25),
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
        trade_price=Decimal("2900.00"),
        position_date=date(2026, 6, 1),
    )
    session.add(p)
    session.commit()
    return p


class TestGenerateFuturesDeliveryObligations:
    def test_long_position_creates_pay_out_obligation(self, db_session):
        contract = _add_futures_contract(db_session)
        _add_position(db_session, contract.contract_id, "BRK-001", BuySell.BUY, 1)

        obligations = generate_futures_delivery_obligations(
            db_session, contract, "INE002A01018", Decimal("2950.00"), date(2026, 6, 26)
        )
        assert len(obligations) == 1
        ob = obligations[0]
        assert ob.net_direction == NetDirection.PAY_OUT
        assert ob.net_quantity == 505
        assert ob.net_value == Decimal("1489750.00")
        assert ob.product_segment == ProductSegment.EQUITY_FO
        assert ob.obligation_stage == ObligationStage.FINAL
        assert ob.isin == "INE002A01018"

    def test_short_position_creates_pay_in_obligation(self, db_session):
        contract = _add_futures_contract(db_session)
        _add_position(db_session, contract.contract_id, "BRK-002", BuySell.SELL, 1)

        obligations = generate_futures_delivery_obligations(
            db_session, contract, "INE002A01018", Decimal("2950.00"), date(2026, 6, 26)
        )
        assert obligations[0].net_direction == NetDirection.PAY_IN

    def test_flat_position_produces_no_obligation(self, db_session):
        contract = _add_futures_contract(db_session)
        _add_position(db_session, contract.contract_id, "BRK-001", BuySell.BUY, 1)
        _add_position(db_session, contract.contract_id, "BRK-001", BuySell.SELL, 1)

        obligations = generate_futures_delivery_obligations(
            db_session, contract, "INE002A01018", Decimal("2950.00"), date(2026, 6, 26)
        )
        assert obligations == []


class TestGenerateOptionDeliveryObligations:
    def test_exercised_long_and_assigned_short_at_strike(self, db_session):
        contract = _add_option_contract(db_session)
        exercise_results = [
            ExerciseResult(
                contract_id=contract.contract_id,
                counterparty_id="BRK-001",
                position_id="POS-1",
                exercised_quantity=1,
                is_itm=True,
            )
        ]
        assignment_results = [
            AssignmentResult(
                contract_id=contract.contract_id,
                counterparty_id="BRK-002",
                position_id="POS-2",
                assigned_quantity=1,
            )
        ]

        obligations = generate_option_delivery_obligations(
            db_session, contract, "INE002A01018", exercise_results, assignment_results, date(2026, 6, 26)
        )
        by_cp = {o.counterparty_id: o for o in obligations}
        assert by_cp["BRK-001"].net_direction == NetDirection.PAY_OUT
        assert by_cp["BRK-001"].vwap_price == Decimal("2900.00")
        assert by_cp["BRK-002"].net_direction == NetDirection.PAY_IN


class TestComputeDeliveryMargin:
    def test_zero_margin_before_ramp_window(self):
        margin = compute_delivery_margin(date(2026, 6, 25), date(2026, 6, 15), Decimal("1000000"))
        assert margin == Decimal("0")

    def test_max_margin_on_expiry_day(self):
        margin = compute_delivery_margin(date(2026, 6, 25), date(2026, 6, 25), Decimal("1000000"))
        assert margin == Decimal("500000.00")

    def test_partial_margin_mid_ramp(self):
        # 2 days to expiry, ramp window = 4 days -> progress = (4-2)/4 = 0.5 -> 25% of notional
        margin = compute_delivery_margin(date(2026, 6, 25), date(2026, 6, 23), Decimal("1000000"))
        assert margin == Decimal("250000.00")

    def test_no_margin_after_expiry(self):
        margin = compute_delivery_margin(date(2026, 6, 25), date(2026, 6, 26), Decimal("1000000"))
        assert margin == Decimal("0")


class TestGetDeliverySummary:
    def test_summary_counts_and_totals(self, db_session):
        contract = _add_futures_contract(db_session)
        _add_position(db_session, contract.contract_id, "BRK-001", BuySell.BUY, 1)
        _add_position(db_session, contract.contract_id, "BRK-002", BuySell.SELL, 1)

        obligations = generate_futures_delivery_obligations(
            db_session, contract, "INE002A01018", Decimal("2950.00"), date(2026, 6, 26)
        )
        summary = get_delivery_summary(obligations)
        assert summary["total"] == 2
        assert summary["pay_in"] == 1
        assert summary["pay_out"] == 1

    def test_empty_summary(self):
        summary = get_delivery_summary([])
        assert summary["total"] == 0
