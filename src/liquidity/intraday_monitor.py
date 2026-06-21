"""
Intraday Liquidity Monitoring Module.

Real-time tracking of settlement flows, net fund positions, and
liquidity stress alerts. Implements programmable settlement controls
per CPMI-IOSCO Principles for Financial Market Infrastructures (PFMI).

Key capabilities:
  - Net fund flow projection per settlement window
  - Liquidity buffer utilization tracking
  - Programmable alerts when thresholds are breached
  - Settlement velocity monitoring (obligations settled per hour)
  - Intraday exposure by counterparty

This is deterministic arithmetic — no LLM reasoning.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.database import Obligation, SettlementInstruction
from src.models.enums import (
    InstructionDirection,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    SettlementCycle,
)


@dataclass
class LiquiditySnapshot:
    timestamp: datetime
    gross_pay_in: Decimal       # total securities/funds to deliver
    gross_pay_out: Decimal      # total securities/funds to receive
    net_position: Decimal       # net fund flow (positive = surplus)
    settled_value: Decimal      # already settled today
    pending_value: Decimal      # yet to settle
    buffer_utilization: float   # % of liquidity buffer used (0-100)


@dataclass
class SettlementVelocity:
    window_start: datetime
    window_end: datetime
    obligations_settled: int
    value_settled: Decimal
    rate_per_hour: float        # obligations per hour


@dataclass
class LiquidityAlert:
    alert_id: str
    timestamp: datetime
    alert_type: str             # BUFFER_BREACH / CONCENTRATION / VELOCITY_DROP / DEADLINE_RISK
    severity: str               # LOW / MEDIUM / HIGH
    message: str
    metric_value: float
    threshold: float


@dataclass
class CounterpartyExposure:
    counterparty_id: str
    gross_exposure: Decimal
    net_exposure: Decimal
    pay_in_value: Decimal
    pay_out_value: Decimal
    pending_count: int


@dataclass
class IntradayLiquidityReport:
    report_date: date
    settlement_cycle: str
    current_snapshot: LiquiditySnapshot
    velocity_windows: list[SettlementVelocity]
    alerts: list[LiquidityAlert]
    counterparty_exposures: list[CounterpartyExposure]
    settlement_progress: float  # % of day's obligations settled


DEFAULT_LIQUIDITY_BUFFER = Decimal("500000000")  # 50 crore INR

ALERT_THRESHOLDS = {
    "buffer_utilization_warning": 70.0,     # % — trigger warning
    "buffer_utilization_critical": 90.0,    # % — trigger critical alert
    "concentration_limit_pct": 30.0,        # % — single counterparty limit
    "velocity_drop_pct": 50.0,              # % — velocity decline trigger
    "deadline_remaining_hours": 2.0,        # hours — settlement deadline proximity
}


def compute_liquidity_snapshot(
    session: Session,
    settlement_date: date,
    current_time: datetime,
    liquidity_buffer: Decimal = DEFAULT_LIQUIDITY_BUFFER,
) -> LiquiditySnapshot:
    """Compute current intraday liquidity position."""
    obligations = (
        session.query(Obligation)
        .filter(
            Obligation.settlement_date == settlement_date,
            Obligation.obligation_stage == ObligationStage.FINAL,
        )
        .all()
    )

    gross_pay_in = Decimal("0")
    gross_pay_out = Decimal("0")
    settled_value = Decimal("0")
    pending_value = Decimal("0")

    for ob in obligations:
        val = abs(ob.net_value)
        if ob.net_direction == NetDirection.PAY_IN:
            gross_pay_in += val
        else:
            gross_pay_out += val

        if ob.status in (ObligationStatus.SETTLED,):
            settled_value += val
        elif ob.status not in (ObligationStatus.FAILED, ObligationStatus.CLOSED_OUT):
            pending_value += val

    net_position = gross_pay_out - gross_pay_in

    if liquidity_buffer > 0:
        utilized = max(Decimal("0"), -net_position)
        utilization = float(utilized / liquidity_buffer * 100)
    else:
        utilization = 0.0

    return LiquiditySnapshot(
        timestamp=current_time,
        gross_pay_in=gross_pay_in,
        gross_pay_out=gross_pay_out,
        net_position=net_position,
        settled_value=settled_value,
        pending_value=pending_value,
        buffer_utilization=round(min(utilization, 100.0), 1),
    )


def compute_settlement_velocity(
    session: Session,
    settlement_date: date,
    window_hours: int = 1,
) -> list[SettlementVelocity]:
    """Compute settlement velocity in hourly windows."""
    settled_obs = (
        session.query(Obligation)
        .filter(
            Obligation.settlement_date == settlement_date,
            Obligation.obligation_stage == ObligationStage.FINAL,
            Obligation.status == ObligationStatus.SETTLED,
        )
        .all()
    )

    market_open = datetime.combine(settlement_date, datetime.min.time().replace(hour=9))
    market_close = datetime.combine(settlement_date, datetime.min.time().replace(hour=16))

    windows = []
    current = market_open

    while current < market_close:
        window_end = current + timedelta(hours=window_hours)

        count = sum(
            1 for ob in settled_obs
            if hasattr(ob, 'computed_at') and ob.computed_at
            and current <= ob.computed_at < window_end
        )

        value = sum(
            abs(ob.net_value) for ob in settled_obs
            if hasattr(ob, 'computed_at') and ob.computed_at
            and current <= ob.computed_at < window_end
        )

        windows.append(SettlementVelocity(
            window_start=current,
            window_end=window_end,
            obligations_settled=count,
            value_settled=value,
            rate_per_hour=count / window_hours,
        ))

        current = window_end

    return windows


def compute_counterparty_exposures(
    session: Session,
    settlement_date: date,
) -> list[CounterpartyExposure]:
    """Compute intraday exposure by counterparty."""
    obligations = (
        session.query(Obligation)
        .filter(
            Obligation.settlement_date == settlement_date,
            Obligation.obligation_stage == ObligationStage.FINAL,
        )
        .all()
    )

    cp_data: dict[str, dict] = {}

    for ob in obligations:
        cp = ob.counterparty_id
        if cp not in cp_data:
            cp_data[cp] = {
                "pay_in": Decimal("0"),
                "pay_out": Decimal("0"),
                "pending": 0,
            }

        val = abs(ob.net_value)
        if ob.net_direction == NetDirection.PAY_IN:
            cp_data[cp]["pay_in"] += val
        else:
            cp_data[cp]["pay_out"] += val

        if ob.status not in (ObligationStatus.SETTLED, ObligationStatus.FAILED, ObligationStatus.CLOSED_OUT):
            cp_data[cp]["pending"] += 1

    exposures = []
    for cp_id, data in cp_data.items():
        gross = data["pay_in"] + data["pay_out"]
        net = data["pay_out"] - data["pay_in"]
        exposures.append(CounterpartyExposure(
            counterparty_id=cp_id,
            gross_exposure=gross,
            net_exposure=net,
            pay_in_value=data["pay_in"],
            pay_out_value=data["pay_out"],
            pending_count=data["pending"],
        ))

    return sorted(exposures, key=lambda e: e.gross_exposure, reverse=True)


def check_alerts(
    snapshot: LiquiditySnapshot,
    exposures: list[CounterpartyExposure],
    velocity_windows: list[SettlementVelocity],
    thresholds: dict | None = None,
) -> list[LiquidityAlert]:
    """Check all liquidity alert conditions."""
    if thresholds is None:
        thresholds = ALERT_THRESHOLDS

    alerts = []
    alert_counter = 0

    # Buffer utilization alerts
    if snapshot.buffer_utilization >= thresholds["buffer_utilization_critical"]:
        alert_counter += 1
        alerts.append(LiquidityAlert(
            alert_id=f"LIQ-{alert_counter:04d}",
            timestamp=snapshot.timestamp,
            alert_type="BUFFER_BREACH",
            severity="HIGH",
            message=f"Liquidity buffer utilization at {snapshot.buffer_utilization:.1f}% — exceeds critical threshold of {thresholds['buffer_utilization_critical']}%",
            metric_value=snapshot.buffer_utilization,
            threshold=thresholds["buffer_utilization_critical"],
        ))
    elif snapshot.buffer_utilization >= thresholds["buffer_utilization_warning"]:
        alert_counter += 1
        alerts.append(LiquidityAlert(
            alert_id=f"LIQ-{alert_counter:04d}",
            timestamp=snapshot.timestamp,
            alert_type="BUFFER_BREACH",
            severity="MEDIUM",
            message=f"Liquidity buffer utilization at {snapshot.buffer_utilization:.1f}% — exceeds warning threshold of {thresholds['buffer_utilization_warning']}%",
            metric_value=snapshot.buffer_utilization,
            threshold=thresholds["buffer_utilization_warning"],
        ))

    # Concentration risk alerts
    if exposures:
        total_gross = sum(e.gross_exposure for e in exposures)
        if total_gross > 0:
            for exp in exposures:
                pct = float(exp.gross_exposure / total_gross * 100)
                if pct >= thresholds["concentration_limit_pct"]:
                    alert_counter += 1
                    alerts.append(LiquidityAlert(
                        alert_id=f"LIQ-{alert_counter:04d}",
                        timestamp=snapshot.timestamp,
                        alert_type="CONCENTRATION",
                        severity="MEDIUM",
                        message=f"Counterparty {exp.counterparty_id} represents {pct:.1f}% of gross exposure — exceeds {thresholds['concentration_limit_pct']}% limit",
                        metric_value=pct,
                        threshold=thresholds["concentration_limit_pct"],
                    ))

    # Velocity drop alerts
    if len(velocity_windows) >= 2:
        recent = velocity_windows[-1]
        prior = velocity_windows[-2]
        if prior.rate_per_hour > 0:
            drop_pct = (1 - recent.rate_per_hour / prior.rate_per_hour) * 100
            if drop_pct >= thresholds["velocity_drop_pct"]:
                alert_counter += 1
                alerts.append(LiquidityAlert(
                    alert_id=f"LIQ-{alert_counter:04d}",
                    timestamp=snapshot.timestamp,
                    alert_type="VELOCITY_DROP",
                    severity="MEDIUM",
                    message=f"Settlement velocity dropped {drop_pct:.0f}% vs prior window ({recent.rate_per_hour:.0f}/hr vs {prior.rate_per_hour:.0f}/hr)",
                    metric_value=drop_pct,
                    threshold=thresholds["velocity_drop_pct"],
                ))

    return alerts


def generate_intraday_report(
    session: Session,
    settlement_date: date,
    current_time: datetime,
    liquidity_buffer: Decimal = DEFAULT_LIQUIDITY_BUFFER,
) -> IntradayLiquidityReport:
    """Generate a comprehensive intraday liquidity report."""
    snapshot = compute_liquidity_snapshot(
        session, settlement_date, current_time, liquidity_buffer
    )

    velocity = compute_settlement_velocity(session, settlement_date)
    exposures = compute_counterparty_exposures(session, settlement_date)
    alerts = check_alerts(snapshot, exposures, velocity)

    total_value = snapshot.settled_value + snapshot.pending_value
    progress = float(snapshot.settled_value / total_value * 100) if total_value > 0 else 0.0

    return IntradayLiquidityReport(
        report_date=settlement_date,
        settlement_cycle="MIXED",
        current_snapshot=snapshot,
        velocity_windows=velocity,
        alerts=alerts,
        counterparty_exposures=exposures,
        settlement_progress=round(progress, 1),
    )
