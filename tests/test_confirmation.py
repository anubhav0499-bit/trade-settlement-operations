"""Unit tests for the custodian confirmation module."""

import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Obligation
from src.models.enums import (
    BreakType,
    ConfirmationStatus,
    CounterpartyType,
    Exchange,
    MatchStatus,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    Severity,
    SettlementCycle,
)
from src.confirmation.custodian_confirm import (
    get_confirmation_cutoff,
    process_confirmations,
    _late_confirmation_severity,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _make_matched_obligation(
    cp_type=CounterpartyType.CUSTODIAN,
    cycle=SettlementCycle.T1,
    settle_date=date(2026, 6, 2),
) -> Obligation:
    return Obligation(
        obligation_id=str(uuid.uuid4()),
        isin="INE002A01018",
        security_name="RELIANCE",
        net_quantity=100,
        net_direction=NetDirection.PAY_OUT,
        vwap_price=Decimal("2900.0000"),
        net_value=Decimal("290000.00"),
        settlement_date=settle_date,
        settlement_cycle=cycle,
        counterparty_id="CUS-001",
        counterparty_type=cp_type,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=ObligationStatus.MATCHED,
        match_status=MatchStatus.MATCHED,
        confirmation_status=ConfirmationStatus.PENDING,
        computed_at=datetime.utcnow(),
        source_trade_ids=json.dumps(["TRD-00001"]),
    )


class TestConfirmationCutoff:
    def test_t1_cutoff(self):
        # Settlement date is T+1 day; cutoff is 13:00 on settlement date
        cutoff = get_confirmation_cutoff(SettlementCycle.T1, date(2026, 6, 2))
        assert cutoff == datetime(2026, 6, 2, 13, 0)

    def test_t0_cutoff(self):
        # Settlement date = trade date for T+0; cutoff is 15:30 on settlement date
        cutoff = get_confirmation_cutoff(SettlementCycle.T0, date(2026, 6, 1))
        assert cutoff == datetime(2026, 6, 1, 15, 30)


class TestProcessConfirmations:
    def test_confirmed_on_time(self, session):
        ob = _make_matched_obligation()
        session.add(ob)
        session.commit()

        responses = {ob.obligation_id: True}
        current = datetime(2026, 6, 2, 12, 0)  # before 1 PM cutoff

        confirmed, problems, breaks = process_confirmations(
            session, [ob], responses, current
        )
        assert len(confirmed) == 1
        assert len(problems) == 0
        assert ob.confirmation_status == ConfirmationStatus.CONFIRMED

    def test_late_confirmation(self, session):
        ob = _make_matched_obligation()
        session.add(ob)
        session.commit()

        responses = {ob.obligation_id: True}
        current = datetime(2026, 6, 2, 14, 0)  # 1 hour past cutoff

        confirmed, problems, breaks = process_confirmations(
            session, [ob], responses, current
        )
        assert len(confirmed) == 0
        assert len(problems) == 1
        assert len(breaks) == 1
        assert breaks[0].break_type == BreakType.LATE_CONFIRMATION

    def test_broker_skips_confirmation(self, session):
        ob = _make_matched_obligation(cp_type=CounterpartyType.BROKER)
        session.add(ob)
        session.commit()

        confirmed, problems, breaks = process_confirmations(
            session, [ob], None, datetime(2026, 6, 2, 12, 0)
        )
        assert len(confirmed) == 1
        assert ob.confirmation_status == ConfirmationStatus.NOT_REQUIRED

    def test_rejected_confirmation(self, session):
        ob = _make_matched_obligation()
        session.add(ob)
        session.commit()

        responses = {ob.obligation_id: False}
        current = datetime(2026, 6, 2, 12, 0)

        confirmed, problems, breaks = process_confirmations(
            session, [ob], responses, current
        )
        assert len(confirmed) == 0
        assert len(problems) == 1
        assert ob.confirmation_status == ConfirmationStatus.REJECTED


class TestLateConfirmationSeverity:
    def test_low(self):
        assert _late_confirmation_severity(15) == Severity.LOW

    def test_medium(self):
        assert _late_confirmation_severity(60) == Severity.MEDIUM

    def test_high(self):
        assert _late_confirmation_severity(180) == Severity.HIGH

    def test_boundary_low_medium(self):
        assert _late_confirmation_severity(30) == Severity.LOW
        assert _late_confirmation_severity(31) == Severity.MEDIUM

    def test_boundary_medium_high(self):
        assert _late_confirmation_severity(120) == Severity.MEDIUM
        assert _late_confirmation_severity(121) == Severity.HIGH
