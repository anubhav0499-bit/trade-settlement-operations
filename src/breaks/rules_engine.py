"""
Break Detection & Classification Rules Engine (§7).

Taxonomy:
  - QUANTITY_MISMATCH
  - PRICE_MISMATCH
  - SSI_MISSING_OR_INCORRECT
  - LATE_CONFIRMATION
  - COUNTERPARTY_FAIL (short delivery)
  - CORPORATE_ACTION_CONFLICT

Severity and aging thresholds are config-driven and cycle-aware:
  - T+0 trades escalate on intraday schedule
  - T+1 trades on standard multi-day schedule
  - LATE_CONFIRMATION uses time-past-cutoff, not value-at-risk
"""

from datetime import datetime

from sqlalchemy.orm import Session

from src.models.database import BreakRecord, Obligation
from src.models.enums import (
    BreakStatus,
    BreakType,
    Severity,
    SettlementCycle,
)
from src.utils.clock import utcnow
from src.utils.config_loader import get_escalation_config


def update_break_aging(
    session: Session,
    current_time: datetime | None = None,
) -> list[BreakRecord]:
    """Update age and escalation for all open breaks."""
    if current_time is None:
        current_time = utcnow()

    config = get_escalation_config()
    updated = []

    open_breaks = (
        session.query(BreakRecord)
        .filter(BreakRecord.status.in_([BreakStatus.OPEN, BreakStatus.IN_PROGRESS]))
        .all()
    )

    for brk in open_breaks:
        obligation = (
            session.query(Obligation)
            .filter(Obligation.obligation_id == brk.obligation_id)
            .first()
        )
        if obligation is None:
            continue

        age_delta = current_time - brk.created_at
        brk.age_hours = age_delta.total_seconds() / 3600
        brk.age_days = age_delta.days

        if brk.break_type == BreakType.LATE_CONFIRMATION:
            _apply_late_confirmation_escalation(brk, config)
        elif obligation.settlement_cycle == SettlementCycle.T0:
            _apply_t0_escalation(brk, config)
        else:
            _apply_t1_escalation(brk, config)

        updated.append(brk)

    session.commit()
    return updated


def _apply_t1_escalation(brk: BreakRecord, config: dict):
    """Apply T+1 escalation thresholds."""
    thresholds = config["t1_cycle"]["aging_thresholds"]
    var_config = config["value_at_risk_severity"]

    base_severity = _var_severity(float(brk.value_at_risk or 0), var_config)

    for threshold in thresholds:
        max_days = threshold.get("max_age_days")
        if max_days is not None and brk.age_days <= max_days:
            min_sev = threshold.get("min_severity")
            if min_sev:
                brk.severity = _max_severity(base_severity, Severity(min_sev))
            else:
                brk.severity = base_severity
            brk.escalation_level = threshold["escalation_level"]
            return

    # Beyond all thresholds
    last = thresholds[-1]
    min_sev = last.get("min_severity")
    if min_sev:
        brk.severity = _max_severity(base_severity, Severity(min_sev))
    brk.escalation_level = last["escalation_level"]


def _apply_t0_escalation(brk: BreakRecord, config: dict):
    """Apply T+0 intraday escalation thresholds."""
    thresholds = config["t0_cycle"]["aging_thresholds"]
    var_config = config["value_at_risk_severity"]

    base_severity = _var_severity(float(brk.value_at_risk or 0), var_config)

    for threshold in thresholds:
        max_hours = threshold.get("max_age_hours")
        if max_hours is not None and (brk.age_hours or 0) <= max_hours:
            min_sev = threshold.get("min_severity")
            if min_sev:
                brk.severity = _max_severity(base_severity, Severity(min_sev))
            else:
                brk.severity = base_severity
            brk.escalation_level = threshold["escalation_level"]
            return

    last = thresholds[-1]
    min_sev = last.get("min_severity")
    if min_sev:
        brk.severity = _max_severity(base_severity, Severity(min_sev))
    brk.escalation_level = last["escalation_level"]


def _apply_late_confirmation_escalation(brk: BreakRecord, config: dict):
    """LATE_CONFIRMATION uses time-past-cutoff, not value-at-risk."""
    lc_config = config["late_confirmation_severity"]
    minutes = (brk.age_hours or 0) * 60

    if minutes <= lc_config["low_max_minutes"]:
        brk.severity = Severity.LOW
        brk.escalation_level = 0
    elif minutes <= lc_config["medium_max_minutes"]:
        brk.severity = Severity.MEDIUM
        brk.escalation_level = 1
    else:
        brk.severity = Severity.HIGH
        brk.escalation_level = 2


def _var_severity(value_at_risk: float, config: dict) -> Severity:
    if value_at_risk < config["low_max"]:
        return Severity.LOW
    elif value_at_risk < config["medium_max"]:
        return Severity.MEDIUM
    return Severity.HIGH


_SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}


def _max_severity(a: Severity, b: Severity) -> Severity:
    return a if _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b] else b


def get_break_summary(session: Session) -> dict:
    """Get a summary of all breaks by type and severity."""
    all_breaks = session.query(BreakRecord).all()

    summary = {
        "total": len(all_breaks),
        "by_type": {},
        "by_severity": {},
        "by_status": {},
    }

    for brk in all_breaks:
        bt = brk.break_type.value
        summary["by_type"][bt] = summary["by_type"].get(bt, 0) + 1

        sev = brk.severity.value
        summary["by_severity"][sev] = summary["by_severity"].get(sev, 0) + 1

        st = brk.status.value
        summary["by_status"][st] = summary["by_status"].get(st, 0) + 1

    return summary
