"""
DvP-I gross settlement for corporate bond / debt trades.

Unlike equity cash (DvP-III, multilateral netting), debt trades settle
gross and bilaterally — each trade's securities leg and funds leg must
both clear independently before that trade is settled. There is no
netting across trades.

Two settlement modes are modeled here:

1. Asynchronous (mark_securities_received / mark_funds_received / settle_dvp1)
   — each leg clears independently, whenever its custodian/bank confirms.
   A trade CAN sit in a partially-cleared state indefinitely (one flag
   True, the other False) — this is how real custodian-mediated DvP-I
   works today.
2. Atomic (settle_dvp_atomic / settle_dvp_atomic_batch) — both legs are
   checked in a single pass; if they aren't simultaneously available,
   NEITHER flag is set and the trade is left completely untouched. This
   models the DLT/PvP "succeed or fail entirely" guarantee atomic
   settlement platforms provide, where there is no observable
   half-settled intermediate state at all.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from datetime import date

from sqlalchemy.orm import Session

from src.models.database import DebtTrade
from src.models.enums import DebtTradeStatus


def _try_settle(trade: DebtTrade) -> None:
    if trade.securities_received and trade.funds_received:
        trade.status = DebtTradeStatus.SETTLED


def mark_securities_received(session: Session, trade_id: str) -> DebtTrade:
    trade = session.query(DebtTrade).filter_by(trade_id=trade_id).one()
    trade.securities_received = True
    _try_settle(trade)
    return trade


def mark_funds_received(session: Session, trade_id: str) -> DebtTrade:
    trade = session.query(DebtTrade).filter_by(trade_id=trade_id).one()
    trade.funds_received = True
    _try_settle(trade)
    return trade


def settle_dvp1(session: Session, trade_id: str) -> DebtTrade:
    """Settle a trade if both legs have already cleared."""
    trade = session.query(DebtTrade).filter_by(trade_id=trade_id).one()
    _try_settle(trade)
    return trade


def check_settlement_failure(trade: DebtTrade, current_date: date) -> bool:
    """Mark a trade FAILED if its settlement date has passed with either leg outstanding."""
    if trade.status == DebtTradeStatus.SETTLED:
        return False
    if current_date > trade.settlement_date and not (
        trade.securities_received and trade.funds_received
    ):
        trade.status = DebtTradeStatus.FAILED
        return True
    return False


def get_settlement_summary(session: Session, as_of_date: date) -> dict:
    trades = session.query(DebtTrade).filter_by(settlement_date=as_of_date).all()
    summary = {"PENDING": 0, "SETTLED": 0, "FAILED": 0}
    for trade in trades:
        summary[trade.status.value] += 1
    return summary


def settle_dvp_atomic(
    session: Session, trade_id: str, securities_available: bool, funds_available: bool,
) -> DebtTrade:
    """Atomic DvP: both legs are evaluated together, in one decision. If
    either leg isn't available right now, the trade is left exactly as it
    was — no flag is set, no partial state is ever recorded. Contrast with
    mark_securities_received/mark_funds_received above, which can legally
    leave securities_received=True and funds_received=False persisted."""
    trade = session.query(DebtTrade).filter_by(trade_id=trade_id).one()
    if securities_available and funds_available:
        trade.securities_received = True
        trade.funds_received = True
        trade.status = DebtTradeStatus.SETTLED
    return trade


def settle_dvp_atomic_batch(
    session: Session,
    trade_ids: list[str],
    securities_availability: dict[str, bool],
    funds_availability: dict[str, bool],
) -> list[DebtTrade]:
    """Run atomic settlement across a batch in one pass — real atomic/DLT
    settlement runs in scheduled windows, not trade-by-trade. A trade absent
    from either availability dict is treated as that leg not being
    available (conservative: missing data never triggers a false settle)."""
    return [
        settle_dvp_atomic(
            session, trade_id,
            securities_availability.get(trade_id, False),
            funds_availability.get(trade_id, False),
        )
        for trade_id in trade_ids
    ]


def get_atomic_settlement_summary(trades: list[DebtTrade]) -> dict:
    if not trades:
        return {"total": 0, "settled": 0, "unsettled": 0}
    settled = sum(1 for t in trades if t.status == DebtTradeStatus.SETTLED)
    return {"total": len(trades), "settled": settled, "unsettled": len(trades) - settled}
