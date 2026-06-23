"""
Delivery Margin Recorder.

The incremental delivery-margin ramp (E-4 to expiry) is already computed by
src/derivatives/physical_delivery.py — this module just turns that amount
into a persisted MarginRecord row tagged MarginType.DELIVERY, so it shows up
alongside SPAN/exposure/VaR margins in margin reports.

All computation is deterministic and rule-based — no LLM reasoning.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.database import MarginRecord
from src.models.enums import MarginType, ProductSegment
from src.derivatives.physical_delivery import compute_delivery_margin


def record_delivery_margin(
    session: Session,
    counterparty_id: str,
    product_segment: ProductSegment,
    expiry_date: date,
    current_date: date,
    notional_value: Decimal,
) -> MarginRecord | None:
    """Compute and persist the delivery margin for one counterparty's position, if any is due."""
    amount = compute_delivery_margin(expiry_date, current_date, notional_value)
    if amount == 0:
        return None

    record = MarginRecord(
        margin_id=str(uuid.uuid4()),
        counterparty_id=counterparty_id,
        product_segment=product_segment,
        margin_type=MarginType.DELIVERY,
        amount=amount,
        as_of_date=current_date,
    )
    session.add(record)
    session.commit()
    return record
