"""Stress tests for intraday liquidity monitoring module."""

import uuid
import pytest
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Obligation
from src.models.enums import (
    CounterpartyType, Exchange, NetDirection,
    ObligationStage, ObligationStatus, SettlementCycle,
)
from src.liquidity.intraday_monitor import (
    LiquiditySnapshot,
    SettlementVelocity,
    check_alerts,
    compute_counterparty_exposures,
    compute_liquidity_snapshot,
    compute_settlement_velocity,
    generate_intraday_report,
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
    net_direction=NetDirection.PAY_IN,
    status=ObligationStatus.PENDING,
    net_value=1_000_000,
    settlement_date=date(2026, 6, 15),
    computed_at=None,
) -> Obligation:
    ob = Obligation(
        obligation_id=str(uuid.uuid4()),
        isin="INE002A01018",
        security_name="TEST",
        net_quantity=100,
        net_direction=net_direction,
        vwap_price=Decimal("2900.00"),
        net_value=Decimal(str(net_value)),
        settlement_date=settlement_date,
        settlement_cycle=SettlementCycle.T1,
        counterparty_id=counterparty_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=status,
        source_trade_ids='["T1"]',
    )
    if computed_at:
        ob.computed_at = computed_at
    session.add(ob)
    session.commit()
    return ob


class TestLiquiditySnapshot:
    def test_balanced_pay_in_out(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_IN, net_value=1_000_000)
        _add_obligation(db_session, net_direction=NetDirection.PAY_OUT, net_value=1_000_000)
        snap = compute_liquidity_snapshot(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 12, 0)
        )
        assert snap.gross_pay_in == Decimal("1000000")
        assert snap.gross_pay_out == Decimal("1000000")
        assert snap.net_position == Decimal("0")

    def test_net_deficit(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_IN, net_value=3_000_000)
        _add_obligation(db_session, net_direction=NetDirection.PAY_OUT, net_value=1_000_000)
        snap = compute_liquidity_snapshot(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 12, 0)
        )
        assert snap.net_position == Decimal("-2000000")

    def test_buffer_utilization_calculation(self, db_session):
        _add_obligation(db_session, net_direction=NetDirection.PAY_IN, net_value=100_000_000)
        snap = compute_liquidity_snapshot(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 12, 0),
            liquidity_buffer=Decimal("500000000"),
        )
        assert snap.buffer_utilization == 20.0  # 100M / 500M * 100

    def test_settled_vs_pending(self, db_session):
        _add_obligation(db_session, status=ObligationStatus.SETTLED, net_value=500_000)
        _add_obligation(db_session, status=ObligationStatus.PENDING, net_value=300_000)
        _add_obligation(db_session, status=ObligationStatus.FAILED, net_value=200_000)
        snap = compute_liquidity_snapshot(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 12, 0)
        )
        assert snap.settled_value == Decimal("500000")
        assert snap.pending_value == Decimal("300000")

    def test_empty_obligations(self, db_session):
        snap = compute_liquidity_snapshot(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 12, 0)
        )
        assert snap.gross_pay_in == Decimal("0")
        assert snap.gross_pay_out == Decimal("0")
        assert snap.buffer_utilization == 0.0


class TestSettlementVelocity:
    def test_hourly_windows(self, db_session):
        windows = compute_settlement_velocity(db_session, date(2026, 6, 15))
        assert len(windows) == 7  # 9am to 4pm = 7 hours

    def test_window_boundaries(self, db_session):
        windows = compute_settlement_velocity(db_session, date(2026, 6, 15))
        assert windows[0].window_start.hour == 9
        assert windows[-1].window_end.hour == 16


class TestCounterpartyExposures:
    def test_single_counterparty(self, db_session):
        _add_obligation(db_session, counterparty_id="BRK-001",
                        net_direction=NetDirection.PAY_IN, net_value=1_000_000)
        _add_obligation(db_session, counterparty_id="BRK-001",
                        net_direction=NetDirection.PAY_OUT, net_value=500_000)
        exposures = compute_counterparty_exposures(db_session, date(2026, 6, 15))
        assert len(exposures) == 1
        exp = exposures[0]
        assert exp.counterparty_id == "BRK-001"
        assert exp.pay_in_value == Decimal("1000000")
        assert exp.pay_out_value == Decimal("500000")
        assert exp.gross_exposure == Decimal("1500000")

    def test_multiple_counterparties_sorted(self, db_session):
        _add_obligation(db_session, counterparty_id="BRK-SMALL", net_value=100_000)
        _add_obligation(db_session, counterparty_id="BRK-BIG", net_value=10_000_000)
        exposures = compute_counterparty_exposures(db_session, date(2026, 6, 15))
        assert exposures[0].counterparty_id == "BRK-BIG"

    def test_pending_count(self, db_session):
        _add_obligation(db_session, status=ObligationStatus.PENDING)
        _add_obligation(db_session, status=ObligationStatus.SETTLED)
        exposures = compute_counterparty_exposures(db_session, date(2026, 6, 15))
        assert exposures[0].pending_count == 1


class TestAlerts:
    def _make_snapshot(self, buffer_util=0.0):
        return LiquiditySnapshot(
            timestamp=datetime(2026, 6, 15, 12, 0),
            gross_pay_in=Decimal("100000000"),
            gross_pay_out=Decimal("80000000"),
            net_position=Decimal("-20000000"),
            settled_value=Decimal("50000000"),
            pending_value=Decimal("30000000"),
            buffer_utilization=buffer_util,
        )

    def test_no_alerts_when_healthy(self):
        snap = self._make_snapshot(buffer_util=50.0)
        alerts = check_alerts(snap, [], [])
        assert len(alerts) == 0

    def test_buffer_warning_alert(self):
        snap = self._make_snapshot(buffer_util=75.0)
        alerts = check_alerts(snap, [], [])
        assert any(a.alert_type == "BUFFER_BREACH" and a.severity == "MEDIUM" for a in alerts)

    def test_buffer_critical_alert(self):
        snap = self._make_snapshot(buffer_util=95.0)
        alerts = check_alerts(snap, [], [])
        assert any(a.alert_type == "BUFFER_BREACH" and a.severity == "HIGH" for a in alerts)

    def test_concentration_alert(self):
        from src.liquidity.intraday_monitor import CounterpartyExposure
        snap = self._make_snapshot()
        exposures = [
            CounterpartyExposure("BRK-BIG", Decimal("90000000"), Decimal("0"),
                                 Decimal("90000000"), Decimal("0"), 5),
            CounterpartyExposure("BRK-SMALL", Decimal("10000000"), Decimal("0"),
                                 Decimal("10000000"), Decimal("0"), 2),
        ]
        alerts = check_alerts(snap, exposures, [])
        assert any(a.alert_type == "CONCENTRATION" for a in alerts)

    def test_velocity_drop_alert(self):
        snap = self._make_snapshot()
        windows = [
            SettlementVelocity(
                datetime(2026, 6, 15, 10, 0), datetime(2026, 6, 15, 11, 0),
                20, Decimal("10000000"), 20.0,
            ),
            SettlementVelocity(
                datetime(2026, 6, 15, 11, 0), datetime(2026, 6, 15, 12, 0),
                5, Decimal("2000000"), 5.0,
            ),
        ]
        alerts = check_alerts(snap, [], windows)
        assert any(a.alert_type == "VELOCITY_DROP" for a in alerts)

    def test_no_velocity_alert_when_improving(self):
        snap = self._make_snapshot()
        windows = [
            SettlementVelocity(
                datetime(2026, 6, 15, 10, 0), datetime(2026, 6, 15, 11, 0),
                10, Decimal("5000000"), 10.0,
            ),
            SettlementVelocity(
                datetime(2026, 6, 15, 11, 0), datetime(2026, 6, 15, 12, 0),
                20, Decimal("10000000"), 20.0,
            ),
        ]
        alerts = check_alerts(snap, [], windows)
        assert not any(a.alert_type == "VELOCITY_DROP" for a in alerts)


class TestIntradayReport:
    def test_report_structure(self, db_session):
        _add_obligation(db_session, status=ObligationStatus.SETTLED, net_value=1_000_000)
        _add_obligation(db_session, status=ObligationStatus.PENDING, net_value=500_000)
        report = generate_intraday_report(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 12, 0)
        )
        assert report.report_date == date(2026, 6, 15)
        assert report.current_snapshot is not None
        assert isinstance(report.velocity_windows, list)
        assert isinstance(report.alerts, list)
        assert isinstance(report.counterparty_exposures, list)
        assert 0.0 <= report.settlement_progress <= 100.0

    def test_progress_calculation(self, db_session):
        _add_obligation(db_session, status=ObligationStatus.SETTLED, net_value=750_000)
        _add_obligation(db_session, status=ObligationStatus.PENDING, net_value=250_000)
        report = generate_intraday_report(
            db_session, date(2026, 6, 15), datetime(2026, 6, 15, 12, 0)
        )
        assert report.settlement_progress == 75.0
