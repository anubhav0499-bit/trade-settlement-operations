"""
SSI Golden-Copy Reference Module (§3).

Validates each obligation's SSI fields against the golden copy before matching.
SSI mismatches are flagged as SSI_MISSING_OR_INCORRECT before the break reaches
the matching engine — this is a reference-data problem, not a trade-data problem.

Supports versioned SSI records (effective-dated) so obligations validate against
the SSI that was current at their settlement date.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from src.models.database import Obligation, SSIRecord, BreakRecord
from src.models.enums import (
    BreakStatus,
    BreakType,
    ObligationStatus,
    Severity,
)

import uuid


@dataclass
class SSIValidationResult:
    obligation_id: str
    is_valid: bool
    issues: list[str]
    ssi_record_used: SSIRecord | None


def get_active_ssi(
    session: Session,
    counterparty_id: str,
    as_of_date: date,
) -> SSIRecord | None:
    """Find the SSI record effective for a given counterparty on a given date."""
    return (
        session.query(SSIRecord)
        .filter(
            SSIRecord.counterparty_id == counterparty_id,
            SSIRecord.effective_from <= as_of_date,
            (SSIRecord.effective_to.is_(None)) | (SSIRecord.effective_to >= as_of_date),
            SSIRecord.is_active.is_(True),
        )
        .first()
    )


def validate_obligation_ssi(
    session: Session,
    obligation: Obligation,
) -> SSIValidationResult:
    """Validate an obligation's counterparty SSI against the golden copy."""
    ssi = get_active_ssi(session, obligation.counterparty_id, obligation.settlement_date)

    if ssi is None:
        return SSIValidationResult(
            obligation_id=obligation.obligation_id,
            is_valid=False,
            issues=[f"No active SSI found for counterparty {obligation.counterparty_id}"],
            ssi_record_used=None,
        )

    issues = []
    if not ssi.dp_id or ssi.dp_id == "MISSING":
        issues.append("DP ID is missing")
    if not ssi.dp_account or ssi.dp_account == "MISSING":
        issues.append("DP account is missing")
    if not ssi.settlement_bank:
        issues.append("Settlement bank is missing")
    if not ssi.bank_account:
        issues.append("Bank account is missing")

    return SSIValidationResult(
        obligation_id=obligation.obligation_id,
        is_valid=len(issues) == 0,
        issues=issues,
        ssi_record_used=ssi,
    )


def validate_all_obligations(
    session: Session,
    obligations: list[Obligation],
) -> tuple[list[Obligation], list[BreakRecord]]:
    """Validate SSI for all obligations. Returns (valid_obligations, ssi_breaks).

    Valid obligations are moved to SSI_VALIDATED status.
    Invalid ones get a BreakRecord created and remain in PENDING status.
    """
    valid = []
    breaks = []

    for ob in obligations:
        if ob.status != ObligationStatus.PENDING:
            continue

        result = validate_obligation_ssi(session, ob)

        if result.is_valid:
            ob.status = ObligationStatus.SSI_VALIDATED
            valid.append(ob)
        else:
            break_record = BreakRecord(
                break_id=str(uuid.uuid4()),
                obligation_id=ob.obligation_id,
                break_type=BreakType.SSI_MISSING_OR_INCORRECT,
                severity=_compute_ssi_severity(ob),
                value_at_risk=ob.net_value,
                age_hours=0,
                age_days=0,
                status=BreakStatus.OPEN,
                escalation_level=0,
            )
            breaks.append(break_record)
            session.add(break_record)

    session.commit()
    return valid, breaks


def _compute_ssi_severity(obligation: Obligation) -> Severity:
    """Compute severity for SSI breaks based on value at risk."""
    val = float(obligation.net_value)
    if val < 500_000:
        return Severity.LOW
    elif val < 2_500_000:
        return Severity.MEDIUM
    return Severity.HIGH
