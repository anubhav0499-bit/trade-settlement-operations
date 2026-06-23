"""Tests for the delivery margin recorder."""

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, MarginRecord
from src.models.enums import MarginType, ProductSegment
from src.margins.delivery_margin import record_delivery_margin


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class TestRecordDeliveryMargin:
    def test_persists_margin_record_within_ramp_window(self, db_session):
        record = record_delivery_margin(
            db_session,
            counterparty_id="BRK-001",
            product_segment=ProductSegment.EQUITY_FO,
            expiry_date=date(2026, 6, 25),
            current_date=date(2026, 6, 25),
            notional_value=Decimal("1000000.00"),
        )
        assert record is not None
        assert record.margin_type == MarginType.DELIVERY
        assert record.amount == Decimal("500000.00")

        stored = db_session.query(MarginRecord).filter_by(margin_id=record.margin_id).first()
        assert stored is not None

    def test_returns_none_outside_ramp_window(self, db_session):
        record = record_delivery_margin(
            db_session,
            counterparty_id="BRK-001",
            product_segment=ProductSegment.EQUITY_FO,
            expiry_date=date(2026, 6, 25),
            current_date=date(2026, 6, 1),
            notional_value=Decimal("1000000.00"),
        )
        assert record is None
        assert db_session.query(MarginRecord).count() == 0
