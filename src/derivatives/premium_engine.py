"""
Options Premium Settlement Engine.

Premium is exchanged once, on the day an option position is opened, and
settles T+1 — separate from the daily MTM cash flow on futures contracts.
Buyers pay premium (debit); sellers receive premium (credit).

All computation is deterministic and rule-based — no LLM reasoning.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import DerivativeContract, DerivativePosition
from src.models.enums import BuySell, ContractType


def compute_premium_obligations(
    session: Session,
    trade_date: date,
) -> dict[str, Decimal]:
    """Net premium payable/receivable per counterparty for option positions opened on trade_date.

    Returns counterparty_id -> net premium (positive = receivable, negative = payable).
    """
    positions = (
        session.query(DerivativePosition)
        .filter(DerivativePosition.position_date == trade_date)
        .all()
    )
    if not positions:
        return {}

    contract_ids = {p.contract_id for p in positions}
    contracts = {
        c.contract_id: c
        for c in session.query(DerivativeContract)
        .filter(
            DerivativeContract.contract_id.in_(contract_ids),
            DerivativeContract.contract_type == ContractType.OPTIONS,
        )
        .all()
    }

    net: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for pos in positions:
        contract = contracts.get(pos.contract_id)
        if contract is None:
            continue  # not an option position — futures carry no premium

        premium = Decimal(str(pos.trade_price)) * pos.quantity * contract.lot_size
        if pos.buy_sell == BuySell.BUY:
            premium = -premium  # buyer pays

        net[pos.counterparty_id] += premium.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return dict(net)


def get_premium_summary(net_premiums: dict[str, Decimal]) -> dict:
    if not net_premiums:
        return {"counterparties": 0, "total_payable": "0", "total_receivable": "0"}
    payable = sum((v for v in net_premiums.values() if v < 0), Decimal("0"))
    receivable = sum((v for v in net_premiums.values() if v > 0), Decimal("0"))
    return {
        "counterparties": len(net_premiums),
        "total_payable": str(payable),
        "total_receivable": str(receivable),
    }
