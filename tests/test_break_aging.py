"""Stress tests for break detection/aging rules engine."""

import uuid
import pytest
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, BreakRecord, Obligation
from src.models.enums import (
    BreakStatus, BreakType, CounterpartyType, Exchange, NetDirection,
    ObligationStage, ObligationStatus, Severity, SettlementCycle,
)
from src.utils.clock import utcnow
from src.breaks.rules_engine import (
    _apply_late_confirmation_escalation,
    _apply_t0_escalation,
    _apply_t1_escalation,
    _max_severity,
    _var_severity,
    get_break_summary,
    update_break_aging,
)
from src.utils.config_loader import get_escalation_config


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def config():
    return get_escalation_config()


def _add_obligation(
    session,
    obligation_id=None,
    settlement_cycle=SettlementCycle.T1,
) -> Obligation:
    oid = obligation_id or str(uuid.uuid4())
    ob = Obligation(
        obligation_id=oid,
        isin="INE002A01018",
        security_name="TEST",
        net_quantity=100,
        net_direction=NetDirection.PAY_IN,
        vwap_price=Decimal("2900.00"),
        net_value=Decimal("290000"),
        settlement_date=utcnow().date(),
        settlement_cycle=settlement_cycle,
        counterparty_id="BRK-001",
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=ObligationStatus.FAILED,
        source_trade_ids='["T1"]',
    )
    session.add(ob)
    session.commit()
    return ob


def _add_break(
    session,
    obligation_id,
    break_type=BreakType.QUANTITY_MISMATCH,
    value_at_risk=290_000,
    created_at=None,
    status=BreakStatus.OPEN,
) -> BreakRecord:
    br = BreakRecord(
        break_id=str(uuid.uuid4()),
        obligation_id=obligation_id,
        break_type=break_type,
        severity=Severity.LOW,
        value_at_risk=Decimal(str(value_at_risk)),
        age_hours=0,
        age_days=0,
        status=status,
        escalation_level=0,
    )
    if created_at:
        br.created_at = created_at
    session.add(br)
    session.commit()
    return br


class TestVARSeverity:
    def test_low(self, config):
        assert _var_severity(100_000, config["value_at_risk_severity"]) == Severity.LOW

    def test_medium(self, config):
        assert _var_severity(1_000_000, config["value_at_risk_severity"]) == Severity.MEDIUM

    def test_high(self, config):
        assert _var_severity(5_000_000, config["value_at_risk_severity"]) == Severity.HIGH

    def test_boundary_low_medium(self, config):
        assert _var_severity(499_999, config["value_at_risk_severity"]) == Severity.LOW
        assert _var_severity(500_000, config["value_at_risk_severity"]) == Severity.MEDIUM

    def test_boundary_medium_high(self, config):
        assert _var_severity(2_499_999, config["value_at_risk_severity"]) == Severity.MEDIUM
        assert _var_severity(2_500_000, config["value_at_risk_severity"]) == Severity.HIGH


class TestMaxSeverity:
    def test_same(self):
        assert _max_severity(Severity.LOW, Severity.LOW) == Severity.LOW

    def test_upgrades(self):
        assert _max_severity(Severity.LOW, Severity.HIGH) == Severity.HIGH
        assert _max_severity(Severity.HIGH, Severity.LOW) == Severity.HIGH

    def test_medium(self):
        assert _max_severity(Severity.LOW, Severity.MEDIUM) == Severity.MEDIUM


class TestT1Escalation:
    def test_day_0_no_bump(self, config):
        brk = BreakRecord(
            break_id="B1", obligation_id="OB1",
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=12, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_t1_escalation(brk, config)
        assert brk.escalation_level == 0
        assert brk.severity == Severity.LOW

    def test_day_2_bumps_to_medium(self, config):
        brk = BreakRecord(
            break_id="B2", obligation_id="OB2",
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=48, age_days=2, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_t1_escalation(brk, config)
        assert brk.escalation_level == 1
        assert brk.severity == Severity.MEDIUM

    def test_day_5_bumps_to_high(self, config):
        brk = BreakRecord(
            break_id="B3", obligation_id="OB3",
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=120, age_days=5, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_t1_escalation(brk, config)
        assert brk.escalation_level == 2
        assert brk.severity == Severity.HIGH

    def test_high_var_stays_high(self, config):
        brk = BreakRecord(
            break_id="B4", obligation_id="OB4",
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW, value_at_risk=Decimal("5000000"),
            age_hours=12, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_t1_escalation(brk, config)
        assert brk.severity == Severity.HIGH


class TestT0Escalation:
    def test_within_4_hours(self, config):
        brk = BreakRecord(
            break_id="B5", obligation_id="OB5",
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=2.0, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_t0_escalation(brk, config)
        assert brk.escalation_level == 0

    def test_5_hours_bumps(self, config):
        brk = BreakRecord(
            break_id="B6", obligation_id="OB6",
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=5.0, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_t0_escalation(brk, config)
        assert brk.escalation_level == 1
        assert brk.severity == Severity.MEDIUM

    def test_10_hours_critical(self, config):
        brk = BreakRecord(
            break_id="B7", obligation_id="OB7",
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=10.0, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_t0_escalation(brk, config)
        assert brk.escalation_level == 2
        assert brk.severity == Severity.HIGH


class TestLateConfirmationEscalation:
    def test_within_30_min(self, config):
        brk = BreakRecord(
            break_id="B8", obligation_id="OB8",
            break_type=BreakType.LATE_CONFIRMATION,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=0.4, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_late_confirmation_escalation(brk, config)
        assert brk.severity == Severity.LOW
        assert brk.escalation_level == 0

    def test_1_hour_medium(self, config):
        brk = BreakRecord(
            break_id="B9", obligation_id="OB9",
            break_type=BreakType.LATE_CONFIRMATION,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=1.0, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_late_confirmation_escalation(brk, config)
        assert brk.severity == Severity.MEDIUM
        assert brk.escalation_level == 1

    def test_3_hours_high(self, config):
        brk = BreakRecord(
            break_id="B10", obligation_id="OB10",
            break_type=BreakType.LATE_CONFIRMATION,
            severity=Severity.LOW, value_at_risk=Decimal("100000"),
            age_hours=3.0, age_days=0, status=BreakStatus.OPEN, escalation_level=0,
        )
        _apply_late_confirmation_escalation(brk, config)
        assert brk.severity == Severity.HIGH
        assert brk.escalation_level == 2


class TestUpdateBreakAging:
    def test_ages_open_breaks(self, db_session):
        ob = _add_obligation(db_session, settlement_cycle=SettlementCycle.T1)
        created = utcnow() - timedelta(hours=48)
        _add_break(db_session, ob.obligation_id, created_at=created)
        updated = update_break_aging(db_session)
        assert len(updated) == 1
        assert updated[0].age_days >= 1

    def test_skips_resolved_breaks(self, db_session):
        ob = _add_obligation(db_session)
        _add_break(db_session, ob.obligation_id, status=BreakStatus.RESOLVED)
        updated = update_break_aging(db_session)
        assert len(updated) == 0

    def test_t0_break_escalates(self, db_session):
        ob = _add_obligation(db_session, settlement_cycle=SettlementCycle.T0)
        created = utcnow() - timedelta(hours=6)
        _add_break(db_session, ob.obligation_id, created_at=created)
        updated = update_break_aging(db_session)
        assert len(updated) == 1
        assert updated[0].escalation_level >= 1


class TestBreakSummary:
    def test_summary_structure(self, db_session):
        ob = _add_obligation(db_session)
        _add_break(db_session, ob.obligation_id, break_type=BreakType.QUANTITY_MISMATCH)
        _add_break(db_session, ob.obligation_id, break_type=BreakType.PRICE_MISMATCH)
        summary = get_break_summary(db_session)
        assert summary["total"] == 2
        assert "QUANTITY_MISMATCH" in summary["by_type"]
        assert "PRICE_MISMATCH" in summary["by_type"]

    def test_empty_summary(self, db_session):
        summary = get_break_summary(db_session)
        assert summary["total"] == 0
