"""Stress tests for counterparty risk scorecard."""

import uuid
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, BreakRecord, Obligation
from src.models.enums import (
    BreakStatus, BreakType, ConfirmationStatus, CounterpartyType, Exchange,
    NetDirection, ObligationStage, ObligationStatus, Severity,
    SettlementCycle,
)
from src.risk.counterparty_scorecard import (
    GRADE_THRESHOLDS,
    _break_frequency_score,
    _concentration_risk_score,
    _fail_history_score,
    _settlement_efficiency_score,
    _timeliness_score,
    compute_all_scorecards,
    compute_scorecard,
    get_scorecard_summary,
    get_watch_list,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_obligation(
    session,
    counterparty_id="BRK-001",
    status=ObligationStatus.SETTLED,
    isin="INE002A01018",
    net_value=1_000_000,
    confirmation_status=ConfirmationStatus.NOT_REQUIRED,
) -> Obligation:
    ob = Obligation(
        obligation_id=str(uuid.uuid4()),
        isin=isin,
        security_name="TEST",
        net_quantity=100,
        net_direction=NetDirection.PAY_IN,
        vwap_price=Decimal("2900.00"),
        net_value=Decimal(str(net_value)),
        settlement_date=date(2026, 6, 1),
        settlement_cycle=SettlementCycle.T1,
        counterparty_id=counterparty_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=status,
        confirmation_status=confirmation_status,
        source_trade_ids='["T1"]',
    )
    session.add(ob)
    session.commit()
    return ob


class TestSettlementEfficiency:
    def test_all_settled(self, db_session):
        for _ in range(10):
            _add_obligation(db_session, status=ObligationStatus.SETTLED)
        dim = _settlement_efficiency_score(db_session, "BRK-001")
        assert dim.score == 100.0

    def test_half_failed(self, db_session):
        for _ in range(5):
            _add_obligation(db_session, status=ObligationStatus.SETTLED)
        for _ in range(5):
            _add_obligation(db_session, status=ObligationStatus.FAILED)
        dim = _settlement_efficiency_score(db_session, "BRK-001")
        assert dim.score == 50.0

    def test_no_obligations_defaults_100(self, db_session):
        dim = _settlement_efficiency_score(db_session, "BRK-EMPTY")
        assert dim.score == 100.0

    def test_weight_is_25pct(self, db_session):
        dim = _settlement_efficiency_score(db_session, "BRK-001")
        assert dim.weight == 0.25


class TestFailHistory:
    def test_no_fails(self, db_session):
        for _ in range(10):
            _add_obligation(db_session, status=ObligationStatus.SETTLED)
        dim = _fail_history_score(db_session, "BRK-001")
        assert dim.score == 100.0

    def test_ten_pct_fails_gives_zero(self, db_session):
        for _ in range(9):
            _add_obligation(db_session, status=ObligationStatus.SETTLED)
        _add_obligation(db_session, status=ObligationStatus.FAILED)
        dim = _fail_history_score(db_session, "BRK-001")
        assert dim.score == 0.0

    def test_five_pct_fails(self, db_session):
        for _ in range(19):
            _add_obligation(db_session, status=ObligationStatus.SETTLED)
        _add_obligation(db_session, status=ObligationStatus.FAILED)
        dim = _fail_history_score(db_session, "BRK-001")
        assert dim.score == pytest.approx(50.0, abs=1.0)


class TestBreakFrequency:
    def test_no_breaks(self, db_session):
        for _ in range(10):
            _add_obligation(db_session)
        dim = _break_frequency_score(db_session, "BRK-001")
        assert dim.score == 100.0

    def test_some_breaks(self, db_session):
        ob = _add_obligation(db_session)
        br = BreakRecord(
            break_id=str(uuid.uuid4()),
            obligation_id=ob.obligation_id,
            break_type=BreakType.QUANTITY_MISMATCH,
            severity=Severity.LOW,
            status=BreakStatus.OPEN,
            escalation_level=0,
        )
        db_session.add(br)
        db_session.commit()
        dim = _break_frequency_score(db_session, "BRK-001")
        assert dim.score == 0.0  # 1 break / 1 obligation = 100 per 100 → 100*5 = 500 → clamped to 0


class TestTimeliness:
    def test_all_confirmed_on_time(self, db_session):
        for _ in range(5):
            _add_obligation(
                db_session,
                confirmation_status=ConfirmationStatus.CONFIRMED,
            )
        dim = _timeliness_score(db_session, "BRK-001")
        assert dim.score == 100.0

    def test_none_required(self, db_session):
        for _ in range(5):
            _add_obligation(
                db_session,
                confirmation_status=ConfirmationStatus.NOT_REQUIRED,
            )
        dim = _timeliness_score(db_session, "BRK-001")
        assert dim.score == 100.0  # defaults to 100 when none require confirmation

    def test_half_late(self, db_session):
        for _ in range(5):
            _add_obligation(
                db_session,
                confirmation_status=ConfirmationStatus.CONFIRMED,
            )
        for _ in range(5):
            _add_obligation(
                db_session,
                confirmation_status=ConfirmationStatus.LATE,
            )
        dim = _timeliness_score(db_session, "BRK-001")
        assert dim.score == 50.0


class TestConcentrationRisk:
    def test_diversified(self, db_session):
        for i in range(5):
            _add_obligation(db_session, isin=f"INE{i:03d}A01018", net_value=1_000_000)
        dim = _concentration_risk_score(db_session, "BRK-001")
        assert dim.score >= 75.0  # well diversified

    def test_single_isin_concentrated(self, db_session):
        for _ in range(10):
            _add_obligation(db_session, isin="INE002A01018", net_value=1_000_000)
        dim = _concentration_risk_score(db_session, "BRK-001")
        assert dim.score == 0.0  # HHI = 1.0

    def test_no_obligations(self, db_session):
        dim = _concentration_risk_score(db_session, "BRK-EMPTY")
        assert dim.score == 100.0


class TestCompositeScorecard:
    def test_perfect_counterparty(self, db_session):
        for i in range(10):
            _add_obligation(
                db_session,
                status=ObligationStatus.SETTLED,
                isin=f"INE{i:03d}A01018",
            )
        sc = compute_scorecard(db_session, "BRK-001")
        assert sc.composite_score >= 80.0
        assert sc.letter_grade == "A"
        assert sc.watch_list is False

    def test_terrible_counterparty(self, db_session):
        for _ in range(10):
            ob = _add_obligation(db_session, status=ObligationStatus.FAILED)
            br = BreakRecord(
                break_id=str(uuid.uuid4()),
                obligation_id=ob.obligation_id,
                break_type=BreakType.QUANTITY_MISMATCH,
                severity=Severity.HIGH,
                status=BreakStatus.OPEN,
                escalation_level=0,
            )
            db_session.add(br)
        db_session.commit()
        sc = compute_scorecard(db_session, "BRK-001")
        assert sc.letter_grade in ("D", "F")
        assert sc.watch_list is True
        assert sc.exposure_limit_multiplier <= 0.5

    def test_grade_thresholds_order(self):
        for threshold, grade, multiplier in GRADE_THRESHOLDS:
            assert 0 <= threshold <= 100
            assert grade in ("A", "B", "C", "D", "F")
            assert 0.0 < multiplier <= 1.5


class TestScorecardBatchAndSummary:
    def test_compute_all_scorecards(self, db_session):
        for cp in ["BRK-001", "BRK-002"]:
            _add_obligation(db_session, counterparty_id=cp)
        cards = compute_all_scorecards(db_session, ["BRK-001", "BRK-002"])
        assert len(cards) == 2

    def test_watch_list_filter(self, db_session):
        for _ in range(10):
            _add_obligation(db_session, counterparty_id="BRK-GOOD", status=ObligationStatus.SETTLED)
        for _ in range(10):
            _add_obligation(db_session, counterparty_id="BRK-BAD", status=ObligationStatus.FAILED)
        cards = compute_all_scorecards(db_session, ["BRK-GOOD", "BRK-BAD"])
        wl = get_watch_list(cards)
        bad_ids = [sc.counterparty_id for sc in wl]
        assert "BRK-BAD" in bad_ids

    def test_summary_structure(self, db_session):
        _add_obligation(db_session, counterparty_id="BRK-001")
        cards = compute_all_scorecards(db_session, ["BRK-001"])
        summary = get_scorecard_summary(cards)
        assert summary["total"] == 1
        assert "by_grade" in summary
        assert "avg_score" in summary

    def test_empty_summary(self):
        summary = get_scorecard_summary([])
        assert summary["total"] == 0
        assert summary["avg_score"] == 0
