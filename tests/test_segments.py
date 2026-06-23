"""Tests for Phase 1 multi-segment foundation."""

import uuid
import pytest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import (
    Base,
    CollateralRecord,
    DerivativeContract,
    DerivativePosition,
    MarginRecord,
    MTMSettlement,
)
from src.models.enums import (
    BuySell,
    CollateralType,
    ContractType,
    DeliveryType,
    MarginType,
    OptionType,
    ProductSegment,
    SettlementCycle,
)
from src.segments.config import get_segment_config


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class TestSegmentConfig:
    def test_equity_cash_is_t1(self):
        cfg = get_segment_config(ProductSegment.EQUITY_CASH)
        assert cfg.settlement_cycle == SettlementCycle.T1
        assert cfg.provisional_cutoff == "21:00"

    def test_all_segments_have_config(self):
        for seg in ProductSegment:
            cfg = get_segment_config(seg)
            assert cfg.product_segment == seg
            assert cfg.settlement_cycle in SettlementCycle


class TestDerivativeContract:
    def test_create_futures_contract(self, db_session):
        c = DerivativeContract(
            contract_id=str(uuid.uuid4()),
            underlying="NIFTY",
            product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.FUTURES,
            option_type=None,
            delivery_type=DeliveryType.CASH,
            strike_price=None,
            lot_size=50,
            expiry_date=date(2026, 6, 25),
        )
        db_session.add(c)
        db_session.commit()
        fetched = db_session.query(DerivativeContract).first()
        assert fetched.underlying == "NIFTY"
        assert fetched.contract_type == ContractType.FUTURES

    def test_create_option_contract(self, db_session):
        c = DerivativeContract(
            contract_id=str(uuid.uuid4()),
            underlying="RELIANCE",
            product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.OPTIONS,
            option_type=OptionType.CALL,
            delivery_type=DeliveryType.PHYSICAL,
            strike_price=2900,
            lot_size=250,
            expiry_date=date(2026, 6, 25),
        )
        db_session.add(c)
        db_session.commit()
        fetched = db_session.query(DerivativeContract).first()
        assert fetched.option_type == OptionType.CALL
        assert fetched.delivery_type == DeliveryType.PHYSICAL


class TestDerivativePosition:
    def test_create_position(self, db_session):
        p = DerivativePosition(
            position_id=str(uuid.uuid4()),
            contract_id="C1",
            counterparty_id="BRK-001",
            buy_sell=BuySell.BUY,
            quantity=10,
            trade_price=125.50,
            position_date=date(2026, 6, 2),
        )
        db_session.add(p)
        db_session.commit()
        fetched = db_session.query(DerivativePosition).first()
        assert fetched.quantity == 10


class TestMTMSettlement:
    def test_create_mtm(self, db_session):
        m = MTMSettlement(
            mtm_id=str(uuid.uuid4()),
            contract_id="C1",
            counterparty_id="BRK-001",
            settlement_date=date(2026, 6, 2),
            settlement_price=22500.0,
            mtm_amount=-1250.0,
        )
        db_session.add(m)
        db_session.commit()
        fetched = db_session.query(MTMSettlement).first()
        assert float(fetched.mtm_amount) == -1250.0


class TestMarginAndCollateral:
    def test_create_margin_record(self, db_session):
        m = MarginRecord(
            margin_id=str(uuid.uuid4()),
            counterparty_id="BRK-001",
            product_segment=ProductSegment.EQUITY_FO,
            margin_type=MarginType.SPAN,
            amount=500000,
            as_of_date=date(2026, 6, 2),
        )
        db_session.add(m)
        db_session.commit()
        fetched = db_session.query(MarginRecord).first()
        assert fetched.margin_type == MarginType.SPAN

    def test_create_collateral_record(self, db_session):
        c = CollateralRecord(
            collateral_id=str(uuid.uuid4()),
            counterparty_id="BRK-001",
            collateral_type=CollateralType.CASH,
            value=1000000,
            haircut_pct=0.0,
            as_of_date=date(2026, 6, 2),
        )
        db_session.add(c)
        db_session.commit()
        fetched = db_session.query(CollateralRecord).first()
        assert fetched.collateral_type == CollateralType.CASH


class TestPipelineDispatch:
    def test_non_equity_cash_raises_not_implemented(self):
        from main import run_pipeline

        with pytest.raises(NotImplementedError):
            run_pipeline(ProductSegment.EQUITY_FO)
