"""
CCIL Integration — read-only G-Sec position reconciliation.

CCIL (Clearing Corporation of India Ltd) is a separate CCP from NSE
Clearing and is the actual settlement venue for G-Sec trades. This module
does NOT settle G-Sec trades — it only reconciles our internally-derived
G-Sec positions (from settled DebtTrade records) against CCIL's reported
positions, which are supplied by the caller (no live CCIL feed in this
system, mirroring the caller-supplied market-data pattern used elsewhere).
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from src.models.database import DebtTrade
from src.models.enums import DebtTradeStatus, ProductSegment


@dataclass
class GSecReconResult:
    counterparty_id: str
    isin: str
    as_of_date: date
    internal_quantity: int
    ccil_quantity: int
    difference: int
    is_reconciled: bool


def derive_gsec_positions(session: Session, as_of_date: date) -> dict[tuple[str, str], int]:
    """Aggregate settled G-Sec DebtTrade rows into net positions per (counterparty, ISIN)."""
    settled = (
        session.query(DebtTrade)
        .filter(
            DebtTrade.product_segment == ProductSegment.DEBT_GSEC,
            DebtTrade.status == DebtTradeStatus.SETTLED,
            DebtTrade.settlement_date <= as_of_date,
        )
        .all()
    )

    positions: dict[tuple[str, str], int] = defaultdict(int)
    for trade in settled:
        positions[(trade.buyer_id, trade.isin)] += trade.quantity
        positions[(trade.seller_id, trade.isin)] -= trade.quantity
    return dict(positions)


def reconcile_ccil_positions(
    session: Session,
    as_of_date: date,
    ccil_positions: dict[tuple[str, str], int],
) -> list[GSecReconResult]:
    """Compare derived internal G-Sec positions against CCIL-reported positions."""
    internal_index = derive_gsec_positions(session, as_of_date)

    all_keys = set(internal_index.keys()) | set(ccil_positions.keys())
    results = []
    for key in sorted(all_keys):
        cp_id, isin = key
        internal_qty = internal_index.get(key, 0)
        ccil_qty = ccil_positions.get(key, 0)
        diff = internal_qty - ccil_qty

        results.append(GSecReconResult(
            counterparty_id=cp_id,
            isin=isin,
            as_of_date=as_of_date,
            internal_quantity=internal_qty,
            ccil_quantity=ccil_qty,
            difference=diff,
            is_reconciled=(diff == 0),
        ))
    return results


def get_gsec_recon_summary(results: list[GSecReconResult]) -> dict:
    total = len(results)
    reconciled = sum(1 for r in results if r.is_reconciled)
    return {
        "total_positions": total,
        "reconciled": reconciled,
        "unreconciled": total - reconciled,
        "total_absolute_difference": sum(abs(r.difference) for r in results),
    }
