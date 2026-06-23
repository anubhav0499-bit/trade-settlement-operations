"""
Expiry-Day Final Settlement.

Cash-settled contracts (index F&O, currency, IRD) get one last MTM leg priced
at the Final Settlement Price (FSP), via the same mtm_engine used for daily
MTM. Physically-settled contracts (stock F&O) are excluded here — their
notional settles via delivery obligations, see physical_delivery.py.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.database import DerivativeContract, MTMSettlement
from src.models.enums import DeliveryType

from src.derivatives.mtm_engine import compute_daily_mtm


def run_final_settlement(
    session: Session,
    expiry_date: date,
    fsp_by_contract: dict[str, Decimal],
) -> list[MTMSettlement]:
    """Run the final MTM leg at FSP for cash-settled contracts expiring on expiry_date."""
    if not fsp_by_contract:
        return []

    contracts = (
        session.query(DerivativeContract)
        .filter(
            DerivativeContract.contract_id.in_(fsp_by_contract.keys()),
            DerivativeContract.expiry_date == expiry_date,
            DerivativeContract.delivery_type == DeliveryType.CASH,
        )
        .all()
    )
    cash_prices = {c.contract_id: fsp_by_contract[c.contract_id] for c in contracts}
    return compute_daily_mtm(session, expiry_date, cash_prices)
