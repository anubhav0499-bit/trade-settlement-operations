"""Stress tests for SSI golden-copy validation module."""

import json
import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Obligation, SSIRecord, BreakRecord
from src.models.enums import (
    BreakType, CounterpartyType, Depository, Exchange,
    MatchStatus, NetDirection, ObligationStage, ObligationStatus, Severity,
)
from src.ssi.golden_copy import (
    get_active_ssi,
    validate_obligation_ssi,
    validate_all_obligations,
    _compute_ssi_severity,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_obligation(
    obligation_id=None,
    counterparty_id="BRK-001",
    settlement_date=date(2026, 6, 15),
    net_value=1_000_000,
    status=ObligationStatus.PENDING,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id or str(uuid.uuid4()),
        isin="INE002A01018",
        security_name="RELIANCE",
        net_quantity=100,
        net_direction=NetDirection.PAY_IN,
        vwap_price=Decimal("2900.00"),
        net_value=Decimal(str(net_value)),
        settlement_date=settlement_date,
        settlement_cycle="T1",
        counterparty_id=counterparty_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=status,
        source_trade_ids='["T1"]',
    )


def _make_ssi(
    counterparty_id="BRK-001",
    effective_from=date(2026, 1, 1),
    effective_to=None,
    is_active=True,
    dp_id="IN300001",
    dp_account="1234567890123456",
    settlement_bank="HDFC Bank",
    bank_account="HDFC00012345",
) -> SSIRecord:
    return SSIRecord(
        ssi_id=str(uuid.uuid4()),
        counterparty_id=counterparty_id,
        settlement_bank=settlement_bank,
        bank_account=bank_account,
        dp_id=dp_id,
        dp_account=dp_account,
        depository=Depository.NSDL,
        effective_from=effective_from,
        effective_to=effective_to,
        is_active=is_active,
    )


class TestGetActiveSSI:
    def test_finds_active_ssi(self, db_session):
        ssi = _make_ssi()
        db_session.add(ssi)
        db_session.commit()
        result = get_active_ssi(db_session, "BRK-001", date(2026, 6, 15))
        assert result is not None
        assert result.counterparty_id == "BRK-001"

    def test_no_ssi_for_unknown_counterparty(self, db_session):
        ssi = _make_ssi(counterparty_id="BRK-001")
        db_session.add(ssi)
        db_session.commit()
        result = get_active_ssi(db_session, "BRK-999", date(2026, 6, 15))
        assert result is None

    def test_ssi_not_yet_effective(self, db_session):
        ssi = _make_ssi(effective_from=date(2026, 7, 1))
        db_session.add(ssi)
        db_session.commit()
        result = get_active_ssi(db_session, "BRK-001", date(2026, 6, 15))
        assert result is None

    def test_ssi_expired(self, db_session):
        ssi = _make_ssi(effective_from=date(2026, 1, 1), effective_to=date(2026, 5, 31))
        db_session.add(ssi)
        db_session.commit()
        result = get_active_ssi(db_session, "BRK-001", date(2026, 6, 15))
        assert result is None

    def test_ssi_inactive(self, db_session):
        ssi = _make_ssi(is_active=False)
        db_session.add(ssi)
        db_session.commit()
        result = get_active_ssi(db_session, "BRK-001", date(2026, 6, 15))
        assert result is None

    def test_effective_date_boundary(self, db_session):
        ssi = _make_ssi(effective_from=date(2026, 6, 15), effective_to=date(2026, 6, 15))
        db_session.add(ssi)
        db_session.commit()
        result = get_active_ssi(db_session, "BRK-001", date(2026, 6, 15))
        assert result is not None

    def test_versioned_ssi_picks_current(self, db_session):
        old = _make_ssi(effective_from=date(2026, 1, 1), effective_to=date(2026, 5, 31),
                        dp_account="OLD_ACCOUNT")
        new = _make_ssi(effective_from=date(2026, 6, 1), dp_account="NEW_ACCOUNT")
        db_session.add_all([old, new])
        db_session.commit()
        result = get_active_ssi(db_session, "BRK-001", date(2026, 6, 15))
        assert result.dp_account == "NEW_ACCOUNT"


class TestValidateObligationSSI:
    def test_valid_obligation(self, db_session):
        ssi = _make_ssi()
        ob = _make_obligation()
        db_session.add_all([ssi, ob])
        db_session.commit()
        result = validate_obligation_ssi(db_session, ob)
        assert result.is_valid is True
        assert len(result.issues) == 0
        assert result.ssi_record_used is not None

    def test_missing_ssi(self, db_session):
        ob = _make_obligation(counterparty_id="BRK-MISSING")
        db_session.add(ob)
        db_session.commit()
        result = validate_obligation_ssi(db_session, ob)
        assert result.is_valid is False
        assert "No active SSI found" in result.issues[0]
        assert result.ssi_record_used is None

    def test_missing_dp_id(self, db_session):
        ssi = _make_ssi(dp_id="MISSING")
        ob = _make_obligation()
        db_session.add_all([ssi, ob])
        db_session.commit()
        result = validate_obligation_ssi(db_session, ob)
        assert result.is_valid is False
        assert any("DP ID" in issue for issue in result.issues)

    def test_missing_dp_account(self, db_session):
        ssi = _make_ssi(dp_account="MISSING")
        ob = _make_obligation()
        db_session.add_all([ssi, ob])
        db_session.commit()
        result = validate_obligation_ssi(db_session, ob)
        assert result.is_valid is False
        assert any("DP account" in issue for issue in result.issues)

    def test_missing_bank(self, db_session):
        ssi = _make_ssi(settlement_bank="")
        ob = _make_obligation()
        db_session.add_all([ssi, ob])
        db_session.commit()
        result = validate_obligation_ssi(db_session, ob)
        assert result.is_valid is False
        assert any("Settlement bank" in issue for issue in result.issues)

    def test_missing_bank_account(self, db_session):
        ssi = _make_ssi(bank_account="")
        ob = _make_obligation()
        db_session.add_all([ssi, ob])
        db_session.commit()
        result = validate_obligation_ssi(db_session, ob)
        assert result.is_valid is False
        assert any("Bank account" in issue for issue in result.issues)

    def test_multiple_issues_aggregated(self, db_session):
        ssi = _make_ssi(dp_id="MISSING", settlement_bank="", bank_account="")
        ob = _make_obligation()
        db_session.add_all([ssi, ob])
        db_session.commit()
        result = validate_obligation_ssi(db_session, ob)
        assert result.is_valid is False
        assert len(result.issues) == 3


class TestValidateAllObligations:
    def test_all_valid(self, db_session):
        ssi = _make_ssi()
        ob1 = _make_obligation(obligation_id="OB-1")
        ob2 = _make_obligation(obligation_id="OB-2")
        db_session.add_all([ssi, ob1, ob2])
        db_session.commit()
        valid, breaks = validate_all_obligations(db_session, [ob1, ob2])
        assert len(valid) == 2
        assert len(breaks) == 0
        assert all(o.status == ObligationStatus.SSI_VALIDATED for o in valid)

    def test_all_invalid(self, db_session):
        ob1 = _make_obligation(obligation_id="OB-1", counterparty_id="BRK-MISSING")
        ob2 = _make_obligation(obligation_id="OB-2", counterparty_id="BRK-MISSING2")
        db_session.add_all([ob1, ob2])
        db_session.commit()
        valid, breaks = validate_all_obligations(db_session, [ob1, ob2])
        assert len(valid) == 0
        assert len(breaks) == 2
        assert all(b.break_type == BreakType.SSI_MISSING_OR_INCORRECT for b in breaks)

    def test_mixed_valid_and_invalid(self, db_session):
        ssi = _make_ssi(counterparty_id="BRK-001")
        ob_valid = _make_obligation(obligation_id="OB-VALID", counterparty_id="BRK-001")
        ob_invalid = _make_obligation(obligation_id="OB-INVALID", counterparty_id="BRK-MISSING")
        db_session.add_all([ssi, ob_valid, ob_invalid])
        db_session.commit()
        valid, breaks = validate_all_obligations(db_session, [ob_valid, ob_invalid])
        assert len(valid) == 1
        assert len(breaks) == 1

    def test_skips_non_pending(self, db_session):
        ssi = _make_ssi()
        ob = _make_obligation(status=ObligationStatus.MATCHED)
        db_session.add_all([ssi, ob])
        db_session.commit()
        valid, breaks = validate_all_obligations(db_session, [ob])
        assert len(valid) == 0
        assert len(breaks) == 0

    def test_break_records_persisted(self, db_session):
        ob = _make_obligation(obligation_id="OB-PERSIST", counterparty_id="BRK-MISSING")
        db_session.add(ob)
        db_session.commit()
        valid, breaks = validate_all_obligations(db_session, [ob])
        stored = db_session.query(BreakRecord).all()
        assert len(stored) == 1
        assert stored[0].obligation_id == "OB-PERSIST"


class TestSSISeverity:
    def test_low_value(self):
        ob = _make_obligation(net_value=100_000)
        assert _compute_ssi_severity(ob) == Severity.LOW

    def test_medium_value(self):
        ob = _make_obligation(net_value=500_000)
        assert _compute_ssi_severity(ob) == Severity.MEDIUM

    def test_high_value(self):
        ob = _make_obligation(net_value=2_500_000)
        assert _compute_ssi_severity(ob) == Severity.HIGH

    def test_boundary_low_to_medium(self):
        ob = _make_obligation(net_value=499_999)
        assert _compute_ssi_severity(ob) == Severity.LOW

    def test_boundary_medium_to_high(self):
        ob = _make_obligation(net_value=2_499_999)
        assert _compute_ssi_severity(ob) == Severity.MEDIUM
