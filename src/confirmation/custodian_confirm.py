"""
Custodian Confirmation Module (§5).

Matched obligations require custodian confirmation before settlement instruction.
Track confirmation cutoff times per settlement cycle. Obligations not confirmed
by cutoff become LATE_CONFIRMATION breaks.

In the Indian market (NSE/BSE), this step is called "custodian confirmation"
(not "affirmation" as in the US/DTCC ecosystem).
"""

import uuid
from datetime import datetime, date, timedelta

from sqlalchemy.orm import Session

from src.models.database import Obligation, BreakRecord
from src.models.enums import (
    BreakStatus,
    BreakType,
    ConfirmationStatus,
    CounterpartyType,
    ObligationStatus,
    SettlementCycle,
    Severity,
)
from src.utils.clock import utcnow
from src.utils.config_loader import get_confirmation_config


def get_confirmation_cutoff(
    settlement_cycle: SettlementCycle,
    settlement_date: date,
    config: dict | None = None,
) -> datetime:
    """Compute the confirmation cutoff datetime for a given cycle and settlement date."""
    if config is None:
        config = get_confirmation_config()

    if settlement_cycle == SettlementCycle.T0:
        cycle_cfg = config["t0_cycle"]
    else:
        cycle_cfg = config["t1_cycle"]

    cutoff_time = datetime.strptime(
        cycle_cfg["confirmation_cutoff_time"], "%H:%M"
    ).time()
    day_offset = cycle_cfg["confirmation_cutoff_day_offset"]
    cutoff_date = settlement_date + timedelta(days=day_offset)

    return datetime.combine(cutoff_date, cutoff_time)


def process_confirmations(
    session: Session,
    obligations: list[Obligation],
    confirmation_responses: dict[str, bool] | None = None,
    current_time: datetime | None = None,
) -> tuple[list[Obligation], list[Obligation], list[BreakRecord]]:
    """Process custodian confirmations for matched obligations.

    Args:
        session: DB session
        obligations: Matched obligations to process
        confirmation_responses: Map of obligation_id → confirmed (True/False).
            If None, simulates responses based on break manifest.
        current_time: Current time for cutoff comparison (defaults to now)

    Returns:
        (confirmed, late/rejected, late_confirmation_breaks)
    """
    if current_time is None:
        current_time = utcnow()

    config = get_confirmation_config()
    confirmed_obs = []
    problem_obs = []
    breaks = []

    for ob in obligations:
        if ob.status != ObligationStatus.MATCHED:
            continue

        # Only custodian-facing obligations need confirmation
        if ob.counterparty_type != CounterpartyType.CUSTODIAN:
            ob.confirmation_status = ConfirmationStatus.NOT_REQUIRED
            ob.status = ObligationStatus.CONFIRMED
            confirmed_obs.append(ob)
            continue

        ob.status = ObligationStatus.PENDING_CONFIRMATION

        cutoff = get_confirmation_cutoff(
            ob.settlement_cycle, ob.settlement_date, config
        )

        if confirmation_responses is not None:
            is_confirmed = confirmation_responses.get(ob.obligation_id, False)
        else:
            # Simulate: most confirmations succeed
            is_confirmed = True

        if is_confirmed and current_time <= cutoff:
            ob.confirmation_status = ConfirmationStatus.CONFIRMED
            ob.status = ObligationStatus.CONFIRMED
            confirmed_obs.append(ob)
        elif is_confirmed and current_time > cutoff:
            ob.confirmation_status = ConfirmationStatus.LATE
            ob.status = ObligationStatus.PENDING_CONFIRMATION
            problem_obs.append(ob)

            minutes_past = (current_time - cutoff).total_seconds() / 60
            severity = _late_confirmation_severity(minutes_past)

            break_record = BreakRecord(
                break_id=str(uuid.uuid4()),
                obligation_id=ob.obligation_id,
                break_type=BreakType.LATE_CONFIRMATION,
                severity=severity,
                value_at_risk=ob.net_value,
                age_hours=minutes_past / 60,
                age_days=0,
                status=BreakStatus.OPEN,
                escalation_level=0,
            )
            breaks.append(break_record)
            session.add(break_record)
        else:
            # Rejected
            ob.confirmation_status = ConfirmationStatus.REJECTED
            problem_obs.append(ob)

    session.commit()
    return confirmed_obs, problem_obs, breaks


def simulate_confirmation_responses(
    obligations: list[Obligation],
    late_rate: float = 0.05,
    reject_rate: float = 0.02,
) -> dict[str, bool]:
    """Generate simulated confirmation responses for testing.

    Most obligations are confirmed on time. A small percentage are late or rejected.
    """
    import random
    random.seed(99)

    responses = {}
    for ob in obligations:
        if ob.counterparty_type != CounterpartyType.CUSTODIAN:
            continue
        roll = random.random()
        if roll < reject_rate:
            responses[ob.obligation_id] = False
        else:
            responses[ob.obligation_id] = True

    return responses


def _late_confirmation_severity(minutes_past_cutoff: float) -> Severity:
    """LATE_CONFIRMATION uses time-past-cutoff, not value-at-risk."""
    if minutes_past_cutoff <= 30:
        return Severity.LOW
    elif minutes_past_cutoff <= 120:
        return Severity.MEDIUM
    return Severity.HIGH
