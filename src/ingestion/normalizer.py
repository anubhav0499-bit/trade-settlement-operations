"""
Trade Capture & Normalization Layer (§1).

Ingests trade records from three simulated sources (OMS, Broker Confirmation,
Custodian Statement), each with a different raw schema, and normalizes them
into the canonical Trade schema for insertion into the unified trade ledger.
"""

import csv
import uuid
from datetime import datetime
from decimal import Decimal
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


def _read_csv(filepath: Path) -> list[dict]:
    with open(filepath, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def normalize_oms_trades(records: list[dict]) -> list[Trade]:
    """OMS records are already close to canonical — direct mapping."""
    trades = []
    for r in records:
        trades.append(Trade(
            trade_id=r["trade_id"],
            isin=r["isin"],
            security_name=r["security_name"],
            quantity=int(r["quantity"]),
            price=Decimal(r["price"]),
            trade_date=datetime.strptime(r["trade_date"], "%Y-%m-%d").date(),
            settlement_date=datetime.strptime(r["settlement_date"], "%Y-%m-%d").date(),
            settlement_cycle=SettlementCycle(r["settlement_cycle"]),
            counterparty_id=r["counterparty_id"],
            counterparty_type=CounterpartyType(r["counterparty_type"]),
            exchange=Exchange(r["exchange"]),
            buy_sell=BuySell(r["buy_sell"]),
            currency=r["currency"],
            source_system=SourceSystem.OMS,
            segment=Segment(r.get("segment", "NORMAL")),
            created_at=datetime.utcnow(),
        ))
    return trades


def normalize_broker_confirmations(records: list[dict]) -> list[Trade]:
    """Broker confirmations use different column names and DD-MMM-YYYY dates."""
    side_map = {"B": BuySell.BUY, "S": BuySell.SELL}
    trades = []
    for r in records:
        trade_date = datetime.strptime(r["TradeDay"], "%d-%b-%Y").date()
        settlement_date = datetime.strptime(r["SettleDay"], "%d-%b-%Y").date()

        trades.append(Trade(
            trade_id=r["TradeRef"],
            isin=r["ISIN_Code"],
            security_name=r["Scrip"].replace("_", " "),
            quantity=int(r["Qty"]),
            price=Decimal(r["Rate"]),
            trade_date=trade_date,
            settlement_date=settlement_date,
            settlement_cycle=SettlementCycle(r["Cycle"]),
            counterparty_id=r["BrokerCode"],
            counterparty_type=CounterpartyType.BROKER,
            exchange=Exchange(r["Exchange_Code"]),
            buy_sell=side_map[r["Side"]],
            currency=r["CCY"],
            source_system=SourceSystem.BROKER_CONFIRM,
            segment=Segment.NORMAL,
            created_at=datetime.utcnow(),
        ))
    return trades


def normalize_custodian_statements(records: list[dict]) -> list[Trade]:
    """Custodian statements use a different schema and ID scheme."""
    trades = []
    for r in records:
        trades.append(Trade(
            trade_id=r["original_trade_ref"],
            isin=r["isin"],
            security_name=r["security_desc"],
            quantity=int(r["qty"]),
            price=Decimal(r["exec_price"]),
            trade_date=datetime.strptime(r["trade_dt"], "%Y-%m-%d").date(),
            settlement_date=datetime.strptime(r["settle_dt"], "%Y-%m-%d").date(),
            settlement_cycle=SettlementCycle(r["cycle"]),
            counterparty_id=r["custodian_code"],
            counterparty_type=CounterpartyType.CUSTODIAN,
            exchange=Exchange(r["exch"]),
            buy_sell=BuySell(r["direction"]),
            currency=r["ccy"],
            source_system=SourceSystem.CUSTODIAN_STATEMENT,
            segment=Segment.NORMAL,
            created_at=datetime.utcnow(),
        ))
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
