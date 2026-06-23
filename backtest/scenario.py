"""
Synthetic multi-day market scenario generator for the backtest harness.

No real historical market data exists in this repo (see data/generated/*.csv,
which are single-day fixtures) — this module generates a deterministic,
seeded sequence of "trading days" with day-over-day price evolution, volume
ramps, and injected stress/default scenarios, so backtest/run_backtest.py can
exercise the pipeline's settlement mechanics across a simulated history
instead of a single static snapshot.

All trades are generated as matched buy/sell pairs (same ISIN, quantity,
price, opposite sides, two different counterparties) so that the market is
balanced by construction — this is what makes the netting conservation check
in backtest/invariants.py meaningful rather than vacuous.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import (
    ClearingMember,
    CollateralRecord,
    DebtTrade,
    DerivativeContract,
    DerivativePosition,
    Trade,
)
from src.models.enums import (
    BuySell,
    CMType,
    CollateralType,
    ContractType,
    CounterpartyType,
    DebtTradeStatus,
    DeliveryType,
    Exchange,
    OptionType,
    ProductSegment,
    Segment,
    SettlementCycle,
    SourceSystem,
)

DEBT_ISIN_CORP = "BT-BOND-CORP"
DEBT_ISIN_GSEC = "BT-BOND-GSEC"

COUNTERPARTIES = [f"BT-CM-{i:03d}" for i in range(1, 21)]
ISINS = [f"INE{i:03d}BT01{i:03d}" for i in range(1, 16)]

NIFTY_FUT = "BT-NIFTY-FUT"
NIFTY_CE = "BT-NIFTY-CE"
RELIANCE_FUT = "BT-RELIANCE-FUT"


def initial_isin_prices(base: Decimal = Decimal("500.00")) -> dict[str, Decimal]:
    return {isin: base for isin in ISINS}


def evolve_prices(prices: dict[str, Decimal], daily_vol: float, rng) -> dict[str, Decimal]:
    """Random-walk each price forward by one day, floored at 1.00."""
    for isin in prices:
        shock = Decimal(str(rng.gauss(0, daily_vol)))
        new_price = prices[isin] * (Decimal("1") + shock)
        prices[isin] = max(new_price, Decimal("1.00")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    return prices


def generate_matched_trades(
    session: Session,
    trade_date: date,
    settle_date: date,
    isin_prices: dict[str, Decimal],
    num_pairs: int,
    rng,
) -> int:
    """Generate num_pairs matched buy/sell trade pairs (balanced market by construction)."""
    trades = []
    for i in range(num_pairs):
        isin = rng.choice(list(isin_prices.keys()))
        price = isin_prices[isin]
        qty = rng.randint(10, 2000)
        buyer, seller = rng.sample(COUNTERPARTIES, 2)
        pair_id = f"{trade_date.isoformat()}-{i:06d}-{uuid.uuid4().hex[:6]}"
        for cp, side in ((buyer, BuySell.BUY), (seller, BuySell.SELL)):
            trades.append(Trade(
                trade_id=f"BT-{pair_id}-{side.value}",
                isin=isin,
                security_name=isin,
                quantity=qty,
                price=price,
                trade_date=trade_date,
                settlement_date=settle_date,
                settlement_cycle=SettlementCycle.T1,
                counterparty_id=cp,
                counterparty_type=CounterpartyType.BROKER,
                exchange=Exchange.NSE,
                buy_sell=side,
                currency="INR",
                source_system=SourceSystem.OMS,
                segment=Segment.NORMAL,
            ))
    session.bulk_save_objects(trades)
    session.commit()
    return len(trades)


def seed_cm_hierarchy(session: Session, count: int = 8) -> list[str]:
    """2 parent TM-CMs, each with (count-2)/2 sub-CMs beneath them."""
    members = [
        ClearingMember(
            cm_id=f"BT-CM-{i:03d}", name=f"Parent CM {i}", cm_type=CMType.TM_CM,
            net_worth=Decimal("50000000"), security_deposit=Decimal("10000000"),
        )
        for i in (1, 2)
    ]
    for i in range(3, count + 1):
        parent_id = f"BT-CM-{1 if i % 2 else 2:03d}"
        members.append(ClearingMember(
            cm_id=f"BT-CM-{i:03d}", name=f"Sub CM {i}", cm_type=CMType.SCM,
            net_worth=Decimal("5000000"), security_deposit=Decimal("1000000"),
            parent_cm_id=parent_id,
        ))
    session.add_all(members)
    session.commit()
    return [f"BT-CM-{i:03d}" for i in range(1, count + 1)]


def seed_collateral(
    session: Session,
    cm_ids: list[str],
    as_of_date: date,
    concentration_violator: str | None = None,
) -> list[CollateralRecord]:
    """Seed a compliant collateral pool for every CM, except optionally tilt
    one CM's portfolio over the equity concentration limit to verify the
    check correctly fires when it should (not just when convenient)."""
    records = []
    for cm_id in cm_ids:
        if cm_id == concentration_violator:
            allocation = [
                (CollateralType.CASH, Decimal("5000000"), 0.0),
                (CollateralType.EQUITY, Decimal("20000000"), 30.0),
            ]
        else:
            # CASH 15M effective; GSEC 0.95M effective (5.7% of total);
            # EQUITY 0.7M effective (4.2% of total) — comfortably inside the
            # 10% concentration limit and the 50% minimum cash rule.
            allocation = [
                (CollateralType.CASH, Decimal("15000000"), 0.0),
                (CollateralType.GOVERNMENT_SECURITY, Decimal("1000000"), 5.0),
                (CollateralType.EQUITY, Decimal("1000000"), 30.0),
            ]
        for j, (ctype, value, haircut) in enumerate(allocation):
            records.append(CollateralRecord(
                collateral_id=f"BT-COL-{cm_id}-{as_of_date.isoformat()}-{j}",
                counterparty_id=cm_id,
                collateral_type=ctype,
                value=value,
                haircut_pct=haircut,
                as_of_date=as_of_date,
            ))
    session.add_all(records)
    session.commit()
    return records


def seed_derivative_book(session: Session, position_date: date, cm_ids: list[str]) -> list[DerivativeContract]:
    """One index future + one index call + one stock future, with positions
    spread across the first 4 CMs — fixed once at the start of the backtest,
    MTM'd daily against the evolving underlying price."""
    expiry = position_date + timedelta(days=60)
    contracts = [
        DerivativeContract(
            contract_id=NIFTY_FUT, underlying="NIFTY", product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.FUTURES, option_type=None, delivery_type=DeliveryType.CASH,
            strike_price=None, lot_size=50, expiry_date=expiry,
        ),
        DerivativeContract(
            contract_id=NIFTY_CE, underlying="NIFTY", product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.OPTIONS, option_type=OptionType.CALL, delivery_type=DeliveryType.CASH,
            strike_price=Decimal("24000"), lot_size=50, expiry_date=expiry,
        ),
        DerivativeContract(
            contract_id=RELIANCE_FUT, underlying="RELIANCE", product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.FUTURES, option_type=None, delivery_type=DeliveryType.PHYSICAL,
            strike_price=None, lot_size=250, expiry_date=expiry,
        ),
    ]
    session.add_all(contracts)
    session.commit()

    positions = [
        (NIFTY_FUT, cm_ids[0], BuySell.BUY, 20, Decimal("23800")),
        (NIFTY_FUT, cm_ids[1], BuySell.SELL, 20, Decimal("23800")),
        (NIFTY_CE, cm_ids[2], BuySell.BUY, 10, Decimal("180")),
        (NIFTY_CE, cm_ids[3], BuySell.SELL, 10, Decimal("180")),
        (RELIANCE_FUT, cm_ids[0], BuySell.BUY, 8, Decimal("1380")),
        (RELIANCE_FUT, cm_ids[2], BuySell.SELL, 8, Decimal("1380")),
    ]
    session.add_all([
        DerivativePosition(
            position_id=f"BT-DPOS-{i:03d}", contract_id=cid, counterparty_id=cp,
            buy_sell=side, quantity=qty, trade_price=price, position_date=position_date,
        )
        for i, (cid, cp, side, qty, price) in enumerate(positions)
    ])
    session.commit()
    return contracts


def generate_debt_trades(
    session: Session, trade_date: date, settle_date: date, num_trades: int, rng,
) -> list[DebtTrade]:
    """A handful of gross DvP-I corporate-bond/G-Sec trades per day, alternating
    instrument type, with distinct buyer/seller — no netting, so unlike equity
    cash there's no conservation invariant to check, only the SETTLED status
    transition once both legs clear."""
    trades = []
    for i in range(num_trades):
        is_gsec = i % 2 == 0
        isin = DEBT_ISIN_GSEC if is_gsec else DEBT_ISIN_CORP
        buyer, seller = rng.sample(COUNTERPARTIES, 2)
        trades.append(DebtTrade(
            trade_id=f"BT-DEBT-{trade_date.isoformat()}-{i:04d}",
            isin=isin,
            buyer_id=buyer,
            seller_id=seller,
            quantity=rng.randint(100, 5000),
            clean_price=Decimal(str(round(rng.uniform(95, 105), 2))),
            trade_date=trade_date,
            settlement_date=settle_date,
            product_segment=ProductSegment.DEBT_GSEC if is_gsec else ProductSegment.DEBT_CORP_BOND,
            source="CCIL" if is_gsec else "CBRICS",
            status=DebtTradeStatus.PENDING,
        ))
    session.add_all(trades)
    session.commit()
    return trades
