"""
Debt Trade Capture & Normalization.

Ingests debt trade records from three simulated sources (CBRICS, RFQ
platform, CCIL trade reports), each with a different raw schema, and
normalizes them into the canonical DebtTrade schema.

Input validation follows the same pattern as src/ingestion/normalizer.py:
records are validated at the system boundary, and invalid records are
logged and skipped rather than crashing the pipeline.
"""

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from src.models.database import DebtTrade
from src.models.enums import DebtTradeStatus, ProductSegment

logger = logging.getLogger(__name__)

_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_MAX_PRICE = Decimal("999999.9999")
_MAX_QUANTITY = 10_000_000


class ValidationError(Exception):
    pass


def _validate_isin(isin: str, record_id: str) -> str:
    isin = isin.strip()
    if not _ISIN_PATTERN.match(isin):
        raise ValidationError(f"Record {record_id}: invalid ISIN format '{isin}'")
    return isin


def _validate_quantity(raw: str, record_id: str) -> int:
    try:
        qty = int(raw)
    except (ValueError, TypeError):
        raise ValidationError(f"Record {record_id}: invalid quantity '{raw}'")
    if qty <= 0 or qty > _MAX_QUANTITY:
        raise ValidationError(f"Record {record_id}: quantity {qty} out of bounds (1-{_MAX_QUANTITY})")
    return qty


def _validate_price(raw: str, record_id: str) -> Decimal:
    try:
        price = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"Record {record_id}: invalid price '{raw}'")
    if price <= 0 or price > _MAX_PRICE:
        raise ValidationError(f"Record {record_id}: price {price} out of bounds")
    return price


def normalize_cbrics_trades(records: list[dict]) -> list[DebtTrade]:
    """CBRICS corporate bond reports — already close to canonical."""
    trades = []
    for r in records:
        record_id = r.get("trade_id", "UNKNOWN")
        try:
            isin = _validate_isin(r["isin"], record_id)
            qty = _validate_quantity(r["quantity"], record_id)
            price = _validate_price(r["price"], record_id)

            trades.append(DebtTrade(
                trade_id=record_id,
                isin=isin,
                buyer_id=r["buyer_id"],
                seller_id=r["seller_id"],
                quantity=qty,
                clean_price=price,
                trade_date=datetime.strptime(r["trade_date"], "%Y-%m-%d").date(),
                settlement_date=datetime.strptime(r["settlement_date"], "%Y-%m-%d").date(),
                product_segment=ProductSegment.DEBT_CORP_BOND,
                source="CBRICS",
                status=DebtTradeStatus.PENDING,
                created_at=datetime.utcnow(),
            ))
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("Skipping invalid CBRICS record %s: %s", record_id, e)
    return trades


def normalize_rfq_trades(records: list[dict]) -> list[DebtTrade]:
    """RFQ platform deals — different column names."""
    trades = []
    for r in records:
        record_id = r.get("DealRef", "UNKNOWN")
        try:
            isin = _validate_isin(r["ISIN"], record_id)
            qty = _validate_quantity(r["Quantity"], record_id)
            price = _validate_price(r["Price"], record_id)

            trades.append(DebtTrade(
                trade_id=record_id,
                isin=isin,
                buyer_id=r["Buyer"],
                seller_id=r["Seller"],
                quantity=qty,
                clean_price=price,
                trade_date=datetime.strptime(r["TradeDate"], "%d-%m-%Y").date(),
                settlement_date=datetime.strptime(r["SettleDate"], "%d-%m-%Y").date(),
                product_segment=ProductSegment.DEBT_CORP_BOND,
                source="RFQ",
                status=DebtTradeStatus.PENDING,
                created_at=datetime.utcnow(),
            ))
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("Skipping invalid RFQ record %s: %s", record_id, e)
    return trades


def normalize_ccil_reports(records: list[dict]) -> list[DebtTrade]:
    """CCIL G-Sec trade reports — settlement itself stays with CCIL; this
    only normalizes the report for our read-only reconciliation (see
    gsec_integration.py)."""
    trades = []
    for r in records:
        record_id = r.get("TradeNo", "UNKNOWN")
        try:
            isin = _validate_isin(r["SecurityCode"], record_id)
            qty = _validate_quantity(r["FaceValueTraded"], record_id)
            price = _validate_price(r["Price"], record_id)

            trades.append(DebtTrade(
                trade_id=record_id,
                isin=isin,
                buyer_id=r["Counterparty1"],
                seller_id=r["Counterparty2"],
                quantity=qty,
                clean_price=price,
                trade_date=datetime.strptime(r["TradeDate"], "%Y-%m-%d").date(),
                settlement_date=datetime.strptime(r["SettleDate"], "%Y-%m-%d").date(),
                product_segment=ProductSegment.DEBT_GSEC,
                source="CCIL",
                status=DebtTradeStatus.PENDING,
                created_at=datetime.utcnow(),
            ))
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("Skipping invalid CCIL record %s: %s", record_id, e)
    return trades
