"""
Reconciliation Module (§11).

End-of-day position reconciliation comparing internal settled positions
(derived from settled obligations) vs custodian EOD holding statements.

Two steps:
1. Position derivation: aggregate settled obligations into running positions
2. Reconciliation: compare derived positions against custodian holdings
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from src.models.database import (
    CustodianHolding,
    Obligation,
    PositionRecord,
)
from src.models.enums import (
    NetDirection,
    ObligationStatus,
)


@dataclass
class ReconResult:
    counterparty_id: str
    isin: str
    statement_date: date
    internal_quantity: int
    custodian_quantity: int
    difference: int
    is_reconciled: bool


def derive_positions(
    session: Session,
    as_of_date: date,
) -> list[PositionRecord]:
    """Derive internal positions from settled obligations.

    Aggregates all obligations with SETTLED status into a position table
    grouped by (counterparty_id, ISIN).
    """
    settled = (
        session.query(Obligation)
        .filter(
            Obligation.status == ObligationStatus.SETTLED,
            Obligation.settlement_date <= as_of_date,
        )
        .all()
    )

    positions: dict[tuple[str, str], int] = defaultdict(int)
    for ob in settled:
        key = (ob.counterparty_id, ob.isin)
        if ob.net_direction == NetDirection.PAY_OUT:
            positions[key] += ob.net_quantity  # received securities
        else:
            positions[key] -= ob.net_quantity  # delivered securities

    records = []
    for (cp_id, isin), qty in positions.items():
        if qty == 0:
            continue
        rec = PositionRecord(
            position_id=str(uuid.uuid4()),
            counterparty_id=cp_id,
            isin=isin,
            quantity=qty,
            as_of_date=as_of_date,
        )
        records.append(rec)
        session.merge(rec)

    session.commit()
    return records


def reconcile_positions(
    session: Session,
    as_of_date: date,
) -> list[ReconResult]:
    """Compare derived positions against custodian EOD holdings."""
    # Derive current internal positions
    internal_positions = derive_positions(session, as_of_date)

    # Index internal positions
    internal_index: dict[tuple[str, str], int] = {}
    for pos in internal_positions:
        internal_index[(pos.counterparty_id, pos.isin)] = pos.quantity

    # Get custodian holdings for this date
    custodian_holdings = (
        session.query(CustodianHolding)
        .filter(CustodianHolding.statement_date == as_of_date)
        .all()
    )

    # Index custodian holdings
    custodian_index: dict[tuple[str, str], int] = {}
    for h in custodian_holdings:
        custodian_index[(h.counterparty_id, h.isin)] = h.quantity

    # Reconcile: union of all keys from both sides
    all_keys = set(internal_index.keys()) | set(custodian_index.keys())
    results = []

    for key in sorted(all_keys):
        cp_id, isin = key
        internal_qty = internal_index.get(key, 0)
        custodian_qty = custodian_index.get(key, 0)
        diff = internal_qty - custodian_qty

        results.append(ReconResult(
            counterparty_id=cp_id,
            isin=isin,
            statement_date=as_of_date,
            internal_quantity=internal_qty,
            custodian_quantity=custodian_qty,
            difference=diff,
            is_reconciled=(diff == 0),
        ))

    return results


def get_recon_summary(results: list[ReconResult]) -> dict:
    """Summarize reconciliation results."""
    total = len(results)
    reconciled = sum(1 for r in results if r.is_reconciled)
    unreconciled = total - reconciled

    return {
        "total_positions": total,
        "reconciled": reconciled,
        "unreconciled": unreconciled,
        "recon_rate": reconciled / total * 100 if total > 0 else 0,
        "total_absolute_difference": sum(abs(r.difference) for r in results),
    }
