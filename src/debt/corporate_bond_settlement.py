"""
DvP-I gross settlement for corporate bond / debt trades.

Unlike equity cash (DvP-III, multilateral netting), debt trades settle
gross and bilaterally — each trade's securities leg and funds leg must
both clear independently before that trade is settled. There is no
netting across trades.

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
