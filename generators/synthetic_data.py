"""
Synthetic data generator for the trade settlement operations system.

Generates ~1,000 trades across 20 trading days with:
- ~15-20% T+0 settlement cycle (restricted to top-500 eligible stocks)
- ~12% break rate with realistic distribution
- 15 counterparty entities (brokers, custodians, clearing corps)
- 50 real NSE/BSE-listed equity ISINs
- Three source formats: OMS, Broker Confirmation, Custodian Statement
- SSI golden copy with version history
- Custodian EOD holding statements for reconciliation
"""

import csv
import json
import os
import random
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from generators.reference_data import (
    COUNTERPARTIES,
    SECURITIES,
    SSI_RECORDS,
    TRADING_DAYS_JUNE_2026,
    CounterpartyInfo,
    SecurityInfo,
    SSIInfo,
)

random.seed(42)

OUTPUT_DIR = Path("data/generated")

# Break type weights (must sum to 1.0)
BREAK_WEIGHTS = {
    "QUANTITY_MISMATCH": 0.30,
    "PRICE_MISMATCH": 0.25,
    "SSI_MISSING_OR_INCORRECT": 0.25,
    "LATE_CONFIRMATION": 0.10,
    "COUNTERPARTY_FAIL": 0.07,
    "CORPORATE_ACTION_CONFLICT": 0.03,
}

TARGET_TRADE_COUNT = 1000
BREAK_RATE = 0.12
T0_RATE = 0.17


def _next_trading_day(trade_date_str: str, offset: int = 1) -> str:
    """Get the next trading day after offset business days."""
    trading_days = [date.fromisoformat(d) for d in TRADING_DAYS_JUNE_2026]
    td = date.fromisoformat(trade_date_str)
    idx = None
    for i, d in enumerate(trading_days):
        if d == td:
            idx = i
            break
    if idx is None:
        # Trade date not in our calendar — estimate
        return (td + timedelta(days=offset)).isoformat()
    target = idx + offset
    if target < len(trading_days):
        return trading_days[target].isoformat()
    # Beyond our window — estimate
    return (trading_days[-1] + timedelta(days=offset)).isoformat()


def _pick_exchange(security: SecurityInfo) -> str:
    if security.exchange == "BOTH":
        return random.choice(["NSE", "BSE"])
    return security.exchange


def _pick_counterparty(exchange: str, cp_type: str) -> CounterpartyInfo:
    eligible = [
        c for c in COUNTERPARTIES
        if c.counterparty_type == cp_type
        and (c.exchange_membership == exchange or c.exchange_membership == "BOTH")
    ]
    return random.choice(eligible)


def _clearing_corp_for_exchange(exchange: str) -> CounterpartyInfo:
    if exchange == "NSE":
        return next(c for c in COUNTERPARTIES if c.counterparty_id == "NSCCL")
    return next(c for c in COUNTERPARTIES if c.counterparty_id == "ICCL")


def _random_price(security: SecurityInfo) -> Decimal:
    price = random.uniform(security.price_low, security.price_high)
    return Decimal(str(price)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _random_quantity() -> int:
    # Lot-like quantities: multiples of common lot sizes
    lots = [1, 5, 10, 25, 50, 100, 200, 500]
    base = random.choice(lots)
    multiplier = random.randint(1, 20)
    return base * multiplier


def _inject_quantity_mismatch(qty: int) -> int:
    delta_pct = random.uniform(0.05, 0.15)
    delta = max(1, int(qty * delta_pct))
    return qty + random.choice([-1, 1]) * delta


def _inject_price_mismatch(price: Decimal) -> Decimal:
    delta_pct = Decimal(str(random.uniform(0.01, 0.03)))
    direction = random.choice([Decimal("1"), Decimal("-1")])
    new_price = price * (Decimal("1") + direction * delta_pct)
    return new_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def generate_trades() -> tuple[list[dict], list[dict], list[dict]]:
    """Generate OMS trades, broker confirmations, and custodian statements.

    Returns three lists of dicts representing the three source formats.
    """
    oms_trades = []
    broker_confirms = []
    custodian_statements = []

    t0_eligible_isins = {s.isin for s in SECURITIES if s.t0_eligible}
    brokers = [c for c in COUNTERPARTIES if c.counterparty_type == "BROKER"]
    custodians = [c for c in COUNTERPARTIES if c.counterparty_type in ("CUSTODIAN",)]

    trades_per_day = TARGET_TRADE_COUNT // len(TRADING_DAYS_JUNE_2026)  # ~50/day
    break_count = int(TARGET_TRADE_COUNT * BREAK_RATE)

    # Pre-assign which trades will be breaks and what type
    break_indices = set(random.sample(range(TARGET_TRADE_COUNT), break_count))
    break_types = random.choices(
        list(BREAK_WEIGHTS.keys()),
        weights=list(BREAK_WEIGHTS.values()),
        k=break_count,
    )
    break_type_map = {}
    for idx, bt in zip(sorted(break_indices), break_types):
        break_type_map[idx] = bt

    trade_idx = 0
    for day_str in TRADING_DAYS_JUNE_2026:
        # Vary trades per day slightly
        n_trades = trades_per_day + random.randint(-8, 8)
        n_trades = max(30, min(70, n_trades))

        for _ in range(n_trades):
            if trade_idx >= TARGET_TRADE_COUNT:
                break

            security = random.choice(SECURITIES)
            exchange = _pick_exchange(security)
            is_t0 = (
                security.isin in t0_eligible_isins
                and random.random() < T0_RATE / 0.65  # adjust for eligible pool
            )
            settlement_cycle = "T0" if is_t0 else "T1"

            if settlement_cycle == "T0":
                settlement_date = day_str
            else:
                settlement_date = _next_trading_day(day_str, 1)

            buy_sell = random.choice(["BUY", "SELL"])
            quantity = _random_quantity()
            price = _random_price(security)

            # Pick a broker and a custodian
            broker = _pick_counterparty(exchange, "BROKER")
            custodian = _pick_counterparty(exchange, "CUSTODIAN")
            clearing_corp = _clearing_corp_for_exchange(exchange)

            trade_id = f"TRD-{trade_idx:05d}"
            is_break = trade_idx in break_indices
            break_type = break_type_map.get(trade_idx)

            # === OMS Record (canonical format) ===
            oms_record = {
                "trade_id": trade_id,
                "isin": security.isin,
                "security_name": security.name,
                "quantity": quantity,
                "price": str(price),
                "trade_date": day_str,
                "settlement_date": settlement_date,
                "settlement_cycle": settlement_cycle,
                "counterparty_id": broker.counterparty_id,
                "counterparty_type": "BROKER",
                "exchange": exchange,
                "buy_sell": buy_sell,
                "currency": "INR",
                "source_system": "OMS",
                "segment": "NORMAL",
            }
            oms_trades.append(oms_record)

            # === Broker Confirmation (different schema) ===
            broker_qty = quantity
            broker_price = price
            broker_ssi_ok = True

            if is_break and break_type == "QUANTITY_MISMATCH":
                broker_qty = _inject_quantity_mismatch(quantity)
            elif is_break and break_type == "PRICE_MISMATCH":
                broker_price = _inject_price_mismatch(price)
            elif is_break and break_type == "SSI_MISSING_OR_INCORRECT":
                broker_ssi_ok = False
            # Other break types don't affect broker confirmation data

            broker_confirm = {
                "ConfirmationID": f"BC-{trade_id}",
                "TradeRef": trade_id,
                "ISIN_Code": security.isin,
                "Scrip": security.name.upper().replace(" ", "_")[:20],
                "Qty": broker_qty,
                "Rate": str(broker_price),
                "TradeDay": datetime.strptime(day_str, "%Y-%m-%d").strftime("%d-%b-%Y"),
                "SettleDay": datetime.strptime(settlement_date, "%Y-%m-%d").strftime("%d-%b-%Y"),
                "Cycle": settlement_cycle,
                "BrokerCode": broker.counterparty_id,
                "BrokerName": broker.name,
                "Exchange_Code": exchange,
                "Side": "B" if buy_sell == "BUY" else "S",
                "CCY": "INR",
                "DP_ID": _get_ssi_field(broker.counterparty_id, "dp_id", broker_ssi_ok),
                "DP_Account": _get_ssi_field(broker.counterparty_id, "dp_account", broker_ssi_ok),
            }
            broker_confirms.append(broker_confirm)

            # === Custodian Statement (subset of fields, different IDs) ===
            cust_qty = quantity
            cust_price = price

            if is_break and break_type == "QUANTITY_MISMATCH":
                # Custodian may also have different qty
                cust_qty = _inject_quantity_mismatch(quantity)
            elif is_break and break_type == "PRICE_MISMATCH":
                cust_price = _inject_price_mismatch(price)

            custodian_stmt = {
                "stmt_ref": f"CS-{trade_id}",
                "original_trade_ref": trade_id,
                "isin": security.isin,
                "security_desc": security.name,
                "qty": cust_qty,
                "exec_price": str(cust_price),
                "trade_dt": day_str,
                "settle_dt": settlement_date,
                "cycle": settlement_cycle,
                "custodian_code": custodian.counterparty_id,
                "custodian_name": custodian.name,
                "exch": exchange,
                "direction": buy_sell,
                "ccy": "INR",
            }
            custodian_statements.append(custodian_stmt)

            trade_idx += 1

        if trade_idx >= TARGET_TRADE_COUNT:
            break

    return oms_trades, broker_confirms, custodian_statements


def _get_ssi_field(counterparty_id: str, field: str, correct: bool) -> str:
    """Get SSI field value; if correct=False, return a wrong value."""
    active_ssi = next(
        (s for s in SSI_RECORDS
         if s.counterparty_id == counterparty_id and s.effective_to is None),
        None,
    )
    if active_ssi is None:
        return "MISSING"

    value = getattr(active_ssi, field)
    if not correct:
        # Inject wrong SSI: swap a few characters
        if len(value) > 4:
            chars = list(value)
            i = random.randint(2, len(chars) - 2)
            chars[i] = str(random.randint(0, 9))
            return "".join(chars)
        return "WRONG_" + value
    return value


def generate_ssi_golden_copy() -> list[dict]:
    """Generate SSI golden copy records."""
    records = []
    for ssi in SSI_RECORDS:
        records.append({
            "ssi_id": str(uuid.uuid4()),
            "counterparty_id": ssi.counterparty_id,
            "settlement_bank": ssi.settlement_bank,
            "bank_account": ssi.bank_account,
            "dp_id": ssi.dp_id,
            "dp_account": ssi.dp_account,
            "depository": ssi.depository,
            "effective_from": ssi.effective_from,
            "effective_to": ssi.effective_to or "",
            "is_active": "1" if ssi.effective_to is None else "0",
        })
    return records


def generate_custodian_holdings(
    oms_trades: list[dict],
) -> list[dict]:
    """Generate synthetic EOD custodian holding statements.

    Derives positions from settled OMS trades and injects ~5% discrepancies
    for reconciliation testing.
    """
    # Aggregate positions by custodian × ISIN × settlement_date
    positions: dict[tuple[str, str, str], int] = defaultdict(int)

    # Map broker trades to custodians for position derivation
    broker_to_custodian = {
        "BRK-001": "CUS-001",
        "BRK-002": "CUS-002",
        "BRK-003": "CUS-003",
        "BRK-004": "CUS-004",
        "BRK-005": "CUS-005",
        "BRK-006": "CUS-001",
        "BC-001": "BC-001",
        "BC-002": "BC-002",
    }

    for trade in oms_trades:
        custodian_id = broker_to_custodian.get(
            trade["counterparty_id"], "CUS-001"
        )
        isin = trade["isin"]
        settle_date = trade["settlement_date"]
        qty = trade["quantity"]
        if trade["buy_sell"] == "BUY":
            positions[(custodian_id, isin, settle_date)] += qty
        else:
            positions[(custodian_id, isin, settle_date)] -= qty

    # Build cumulative positions per custodian × ISIN across dates
    cumulative: dict[tuple[str, str], int] = defaultdict(int)
    holdings = []
    all_dates = sorted(set(t[2] for t in positions.keys()))

    for stmt_date in all_dates:
        # Update cumulative from this date's settlements
        for (cust, isin, sd), qty in positions.items():
            if sd == stmt_date:
                cumulative[(cust, isin)] += qty

        # Emit holdings for each custodian × ISIN with non-zero position
        for (cust, isin), qty in cumulative.items():
            if qty == 0:
                continue

            reported_qty = qty
            # 5% discrepancy injection
            if random.random() < 0.05:
                delta = max(1, abs(qty) // 10)
                reported_qty = qty + random.choice([-1, 1]) * delta

            sec = next((s for s in SECURITIES if s.isin == isin), None)
            holdings.append({
                "holding_id": str(uuid.uuid4()),
                "counterparty_id": cust,
                "isin": isin,
                "security_name": sec.name if sec else "UNKNOWN",
                "quantity": reported_qty,
                "statement_date": stmt_date,
                "source": "CUSTODIAN_EOD_STATEMENT",
            })

    return holdings


def generate_counterparty_master() -> list[dict]:
    """Generate counterparty master records."""
    return [
        {
            "counterparty_id": c.counterparty_id,
            "name": c.name,
            "counterparty_type": c.counterparty_type,
            "exchange_membership": c.exchange_membership,
            "is_active": "1",
        }
        for c in COUNTERPARTIES
    ]


def _write_csv(records: list[dict], filename: str):
    if not records:
        return
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    print(f"  Written {len(records)} records to {filepath}")


def _build_break_manifest(
    oms_trades: list[dict],
    broker_confirms: list[dict],
    custodian_statements: list[dict],
) -> list[dict]:
    """Build a manifest of injected breaks for validation."""
    break_count = int(len(oms_trades) * BREAK_RATE)
    break_indices = set(random.seed(42) or [])  # Re-seed for consistency

    random.seed(42)
    break_indices = set(random.sample(range(len(oms_trades)), break_count))
    break_types = random.choices(
        list(BREAK_WEIGHTS.keys()),
        weights=list(BREAK_WEIGHTS.values()),
        k=break_count,
    )

    manifest = []
    for idx, bt in zip(sorted(break_indices), break_types):
        oms = oms_trades[idx]
        broker = broker_confirms[idx]
        cust = custodian_statements[idx]

        entry = {
            "trade_id": oms["trade_id"],
            "isin": oms["isin"],
            "break_type": bt,
            "oms_qty": oms["quantity"],
            "broker_qty": broker["Qty"],
            "custodian_qty": cust["qty"],
            "oms_price": oms["price"],
            "broker_price": broker["Rate"],
            "custodian_price": cust["exec_price"],
        }
        manifest.append(entry)

    return manifest


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Generating synthetic trade settlement data...")
    print(f"  Target: {TARGET_TRADE_COUNT} trades, {BREAK_RATE*100:.0f}% break rate")
    print(f"  T+0 rate: {T0_RATE*100:.0f}%, Trading days: {len(TRADING_DAYS_JUNE_2026)}")
    print()

    # Generate trades from three sources
    oms_trades, broker_confirms, custodian_stmts = generate_trades()

    # Count T+0 vs T+1
    t0_count = sum(1 for t in oms_trades if t["settlement_cycle"] == "T0")
    t1_count = len(oms_trades) - t0_count
    print(f"  Generated {len(oms_trades)} OMS trades ({t0_count} T+0, {t1_count} T+1)")

    # Write trade CSVs
    _write_csv(oms_trades, "oms_trades.csv")
    _write_csv(broker_confirms, "broker_confirmations.csv")
    _write_csv(custodian_stmts, "custodian_statements.csv")

    # SSI golden copy
    ssi_records = generate_ssi_golden_copy()
    _write_csv(ssi_records, "ssi_golden_copy.csv")
    print(f"  Generated {len(ssi_records)} SSI records ({sum(1 for s in ssi_records if s['is_active'] == '1')} active)")

    # Counterparty master
    cp_master = generate_counterparty_master()
    _write_csv(cp_master, "counterparty_master.csv")

    # Custodian holdings for recon
    holdings = generate_custodian_holdings(oms_trades)
    _write_csv(holdings, "custodian_holdings.csv")

    # Break manifest (for validation)
    manifest = _build_break_manifest(oms_trades, broker_confirms, custodian_stmts)
    _write_csv(manifest, "break_manifest.csv")
    print(f"  Break manifest: {len(manifest)} injected breaks")

    # Summary statistics
    print("\n=== Generation Summary ===")
    print(f"  Total trades: {len(oms_trades)}")
    print(f"  T+0 trades: {t0_count} ({t0_count/len(oms_trades)*100:.1f}%)")
    print(f"  T+1 trades: {t1_count} ({t1_count/len(oms_trades)*100:.1f}%)")
    print(f"  Unique ISINs: {len(set(t['isin'] for t in oms_trades))}")
    all_cps = (
        set(t["counterparty_id"] for t in oms_trades)
        | set(t["BrokerCode"] for t in broker_confirms)
        | set(t["custodian_code"] for t in custodian_stmts)
    )
    print(f"  Unique counterparties: {len(all_cps)} (across all sources)")
    print(f"  Trading days covered: {len(set(t['trade_date'] for t in oms_trades))}")
    print(f"  Injected breaks: {len(manifest)}")

    # Break type distribution
    from collections import Counter
    bt_dist = Counter(m["break_type"] for m in manifest)
    print("\n  Break type distribution:")
    for bt, count in bt_dist.most_common():
        print(f"    {bt}: {count} ({count/len(manifest)*100:.1f}%)")

    # Exchange distribution
    exch_dist = Counter(t["exchange"] for t in oms_trades)
    print(f"\n  Exchange distribution:")
    for ex, count in exch_dist.most_common():
        print(f"    {ex}: {count} ({count/len(oms_trades)*100:.1f}%)")

    print(f"\n  Custodian holdings: {len(holdings)} position records")
    discrepancy_note = sum(
        1 for h in holdings
        # Can't easily count discrepancies without re-running, but ~5% injected
    )
    print(f"  SSI records: {len(ssi_records)} ({len([s for s in SSI_RECORDS if s.effective_to])} historical versions)")

    print(f"\nAll files written to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
