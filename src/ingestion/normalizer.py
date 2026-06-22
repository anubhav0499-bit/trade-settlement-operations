"""
Trade Capture & Normalization Layer (§1).

Ingests trade records from three simulated sources (OMS, Broker Confirmation,
Custodian Statement), each with a different raw schema, and normalizes them
into the canonical Trade schema for insertion into the unified trade ledger.

Input validation: all records are validated at the system boundary before
ORM object construction. Invalid records are logged and skipped rather
than crashing the pipeline.
"""

import csv
import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.orm import Session

from src.models.database import Trade, Counterparty, SSIRecord, CustodianHolding, create_tables, get_engine, get_session
from src.models.enums import (
    BuySell,
    CounterpartyType,
    Depository,
    Exchange,
    Segment,
    SettlementCycle,
    SourceSystem,
)

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


def _validate_enum(enum_cls, raw: str, field_name: str, record_id: str):
    try:
        return enum_cls(raw)
    except (ValueError, KeyError):
        valid = [e.value for e in enum_cls]
        raise ValidationError(f"Record {record_id}: invalid {field_name} '{raw}', expected one of {valid}")


def _read_csv(filepath: Path) -> list[dict]:
    with open(filepath, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def normalize_oms_trades(records: list[dict]) -> list[Trade]:
    """OMS records are already close to canonical — direct mapping with validation."""
    trades = []
    for r in records:
        record_id = r.get("trade_id", "UNKNOWN")
        try:
            isin = _validate_isin(r["isin"], record_id)
            qty = _validate_quantity(r["quantity"], record_id)
            price = _validate_price(r["price"], record_id)
            cycle = _validate_enum(SettlementCycle, r["settlement_cycle"], "settlement_cycle", record_id)
            cp_type = _validate_enum(CounterpartyType, r["counterparty_type"], "counterparty_type", record_id)
            exchange = _validate_enum(Exchange, r["exchange"], "exchange", record_id)
            side = _validate_enum(BuySell, r["buy_sell"], "buy_sell", record_id)
            segment = _validate_enum(Segment, r.get("segment", "NORMAL"), "segment", record_id)

            trades.append(Trade(
                trade_id=record_id,
                isin=isin,
                security_name=r["security_name"],
                quantity=qty,
                price=price,
                trade_date=datetime.strptime(r["trade_date"], "%Y-%m-%d").date(),
                settlement_date=datetime.strptime(r["settlement_date"], "%Y-%m-%d").date(),
                settlement_cycle=cycle,
                counterparty_id=r["counterparty_id"],
                counterparty_type=cp_type,
                exchange=exchange,
                buy_sell=side,
                currency=r["currency"],
                source_system=SourceSystem.OMS,
                segment=segment,
                created_at=datetime.utcnow(),
            ))
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("Skipping invalid OMS record %s: %s", record_id, e)
    return trades


def normalize_broker_confirmations(records: list[dict]) -> list[Trade]:
    """Broker confirmations use different column names and DD-MMM-YYYY dates."""
    side_map = {"B": BuySell.BUY, "S": BuySell.SELL}
    trades = []
    for r in records:
        record_id = r.get("TradeRef", "UNKNOWN")
        try:
            isin = _validate_isin(r["ISIN_Code"], record_id)
            qty = _validate_quantity(r["Qty"], record_id)
            price = _validate_price(r["Rate"], record_id)
            cycle = _validate_enum(SettlementCycle, r["Cycle"], "Cycle", record_id)
            exchange = _validate_enum(Exchange, r["Exchange_Code"], "Exchange_Code", record_id)

            side_raw = r["Side"]
            if side_raw not in side_map:
                raise ValidationError(f"Record {record_id}: invalid Side '{side_raw}', expected B or S")

            trade_date = datetime.strptime(r["TradeDay"], "%d-%b-%Y").date()
            settlement_date = datetime.strptime(r["SettleDay"], "%d-%b-%Y").date()

            trades.append(Trade(
                trade_id=record_id,
                isin=isin,
                security_name=r["Scrip"].replace("_", " "),
                quantity=qty,
                price=price,
                trade_date=trade_date,
                settlement_date=settlement_date,
                settlement_cycle=cycle,
                counterparty_id=r["BrokerCode"],
                counterparty_type=CounterpartyType.BROKER,
                exchange=exchange,
                buy_sell=side_map[side_raw],
                currency=r["CCY"],
                source_system=SourceSystem.BROKER_CONFIRM,
                segment=Segment.NORMAL,
                created_at=datetime.utcnow(),
            ))
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("Skipping invalid broker record %s: %s", record_id, e)
    return trades


def normalize_custodian_statements(records: list[dict]) -> list[Trade]:
    """Custodian statements use a different schema and ID scheme."""
    trades = []
    for r in records:
        record_id = r.get("original_trade_ref", "UNKNOWN")
        try:
            isin = _validate_isin(r["isin"], record_id)
            qty = _validate_quantity(r["qty"], record_id)
            price = _validate_price(r["exec_price"], record_id)
            cycle = _validate_enum(SettlementCycle, r["cycle"], "cycle", record_id)
            exchange = _validate_enum(Exchange, r["exch"], "exch", record_id)
            side = _validate_enum(BuySell, r["direction"], "direction", record_id)

            trades.append(Trade(
                trade_id=record_id,
                isin=isin,
                security_name=r["security_desc"],
                quantity=qty,
                price=price,
                trade_date=datetime.strptime(r["trade_dt"], "%Y-%m-%d").date(),
                settlement_date=datetime.strptime(r["settle_dt"], "%Y-%m-%d").date(),
                settlement_cycle=cycle,
                counterparty_id=r["custodian_code"],
                counterparty_type=CounterpartyType.CUSTODIAN,
                exchange=exchange,
                buy_sell=side,
                currency=r["ccy"],
                source_system=SourceSystem.CUSTODIAN_STATEMENT,
                segment=Segment.NORMAL,
                created_at=datetime.utcnow(),
            ))
        except (ValidationError, KeyError, ValueError) as e:
            logger.warning("Skipping invalid custodian record %s: %s", record_id, e)
    return trades


def load_counterparty_master(records: list[dict], session: Session):
    for r in records:
        cp = Counterparty(
            counterparty_id=r["counterparty_id"],
            name=r["name"],
            counterparty_type=CounterpartyType(r["counterparty_type"]),
            exchange_membership=r["exchange_membership"],
            is_active=r["is_active"] == "1",
        )
        session.merge(cp)
    session.commit()


def load_ssi_golden_copy(records: list[dict], session: Session):
    for r in records:
        ssi = SSIRecord(
            ssi_id=r["ssi_id"],
            counterparty_id=r["counterparty_id"],
            settlement_bank=r["settlement_bank"],
            bank_account=r["bank_account"],
            dp_id=r["dp_id"],
            dp_account=r["dp_account"],
            depository=Depository(r["depository"]),
            effective_from=datetime.strptime(r["effective_from"], "%Y-%m-%d").date(),
            effective_to=(
                datetime.strptime(r["effective_to"], "%Y-%m-%d").date()
                if r["effective_to"]
                else None
            ),
            is_active=r["is_active"] == "1",
        )
        session.merge(ssi)
    session.commit()


def load_custodian_holdings(records: list[dict], session: Session):
    for r in records:
        holding = CustodianHolding(
            holding_id=r["holding_id"],
            counterparty_id=r["counterparty_id"],
            isin=r["isin"],
            quantity=int(r["quantity"]),
            statement_date=datetime.strptime(r["statement_date"], "%Y-%m-%d").date(),
            source=r["source"],
        )
        session.merge(holding)
    session.commit()


def ingest_all(data_dir: Path, db_path: str = "data/generated/settlement.db"):
    """Full ingestion pipeline: read CSVs, normalize, load into SQLite."""
    engine = get_engine(db_path)
    create_tables(engine)
    session = get_session(engine)

    # Load reference data
    cp_records = _read_csv(data_dir / "counterparty_master.csv")
    load_counterparty_master(cp_records, session)
    print(f"  Loaded {len(cp_records)} counterparties")

    ssi_records = _read_csv(data_dir / "ssi_golden_copy.csv")
    load_ssi_golden_copy(ssi_records, session)
    print(f"  Loaded {len(ssi_records)} SSI records")

    # Load trades from all three sources (each gets a unique trade_id suffix)
    oms_raw = _read_csv(data_dir / "oms_trades.csv")
    oms_trades = normalize_oms_trades(oms_raw)
    for t in oms_trades:
        session.merge(t)
    session.commit()
    print(f"  Ingested {len(oms_trades)} OMS trades")

    # Broker confirmations get source-prefixed IDs to avoid PK collision
    broker_raw = _read_csv(data_dir / "broker_confirmations.csv")
    broker_trades = normalize_broker_confirmations(broker_raw)
    for t in broker_trades:
        t.trade_id = f"BRK-{t.trade_id}"
        session.merge(t)
    session.commit()
    print(f"  Ingested {len(broker_trades)} broker confirmation trades")

    # Custodian statements
    cust_raw = _read_csv(data_dir / "custodian_statements.csv")
    cust_trades = normalize_custodian_statements(cust_raw)
    for t in cust_trades:
        t.trade_id = f"CUS-{t.trade_id}"
        session.merge(t)
    session.commit()
    print(f"  Ingested {len(cust_trades)} custodian statement trades")

    # Load custodian holdings for recon
    holdings_raw = _read_csv(data_dir / "custodian_holdings.csv")
    load_custodian_holdings(holdings_raw, session)
    print(f"  Loaded {len(holdings_raw)} custodian holding records")

    total = session.query(Trade).count()
    print(f"\n  Total trades in ledger: {total}")

    session.close()
    return engine
