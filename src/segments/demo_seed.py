"""Deterministic synthetic seed data for the non-equity-cash segments.

Equity cash trades come from committed CSV fixtures (see
src/ingestion/normalizer.py) — no equivalent fixtures exist yet for
F&O/currency derivatives/IRD/debt, so this module generates a small, fixed
set of representative ORM records directly, reusing the existing equity
cash counterparty universe (BRK-xxx) rather than inventing a new one. This
gives the Phase 2-5 engines real DB rows to run against in the pipeline.

All record generation here is deterministic (no randomness) — these are
fixtures, not a simulation of market activity.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.cm_hierarchy.hierarchy import register_clearing_member
from src.models.database import (
    DebtInstrument,
    DebtTrade,
    DerivativeContract,
    DerivativePosition,
    CollateralRecord,
    Trade,
)
from src.models.enums import (
    BuySell,
    CMType,
    CollateralType,
    ContractType,
    CounterpartyType,
    DayCountConvention,
    DebtInstrumentType,
    DebtTradeStatus,
    DeliveryType,
    Exchange,
    OptionType,
    ProductSegment,
    SettlementCycle,
    SourceSystem,
)

CM_IDS = ["BRK-001", "BRK-002", "BRK-003", "BRK-004", "BRK-005", "BRK-006"]

# Contract IDs, exposed so callers (main.py) can pass settlement/FSP prices.
NIFTY_FUT = "NIFTY-FUT-20260625"
NIFTY_CE = "NIFTY-CE-24000-20260625"
RELIANCE_FUT = "RELIANCE-FUT-20260625"
RELIANCE_CE = "RELIANCE-CE-1400-20260625"
EXPIRY_DATE = date(2026, 6, 25)

CORP_BOND_ISIN = "INE002A08534"
GSEC_ISIN = "IN0020230012"


def seed_clearing_members(session: Session) -> list[str]:
    """A small TM-CM hierarchy: 2 parent CMs, each clearing for sub-TMs."""
    register_clearing_member(session, "BRK-001", "Zerodha Securities", CMType.TM_CM, Decimal("50000000"), Decimal("10000000"))
    register_clearing_member(session, "BRK-002", "ICICI Securities", CMType.TM_CM, Decimal("80000000"), Decimal("15000000"))
    register_clearing_member(session, "BRK-003", "HDFC Securities", CMType.SCM, Decimal("5000000"), Decimal("1000000"), parent_cm_id="BRK-001")
    register_clearing_member(session, "BRK-004", "Kotak Securities", CMType.SCM, Decimal("6000000"), Decimal("1200000"), parent_cm_id="BRK-001")
    register_clearing_member(session, "BRK-005", "Motilal Oswal Securities", CMType.PCM, Decimal("4000000"), Decimal("800000"), parent_cm_id="BRK-002")
    register_clearing_member(session, "BRK-006", "Axis Securities", CMType.PCM, Decimal("3500000"), Decimal("700000"), parent_cm_id="BRK-002")
    return CM_IDS


def seed_derivative_contracts_and_positions(session: Session, position_date: date) -> None:
    """Two index contracts (cash-settled) and two stock contracts (physically settled)."""
    contracts = [
        DerivativeContract(
            contract_id=NIFTY_FUT, underlying="NIFTY", product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.FUTURES, option_type=None, delivery_type=DeliveryType.CASH,
            strike_price=None, lot_size=50, expiry_date=EXPIRY_DATE,
        ),
        DerivativeContract(
            contract_id=NIFTY_CE, underlying="NIFTY", product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.OPTIONS, option_type=OptionType.CALL, delivery_type=DeliveryType.CASH,
            strike_price=Decimal("24000"), lot_size=50, expiry_date=EXPIRY_DATE,
        ),
        DerivativeContract(
            contract_id=RELIANCE_FUT, underlying="RELIANCE", product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.FUTURES, option_type=None, delivery_type=DeliveryType.PHYSICAL,
            strike_price=None, lot_size=250, expiry_date=EXPIRY_DATE,
        ),
        DerivativeContract(
            contract_id=RELIANCE_CE, underlying="RELIANCE", product_segment=ProductSegment.EQUITY_FO,
            contract_type=ContractType.OPTIONS, option_type=OptionType.CALL, delivery_type=DeliveryType.PHYSICAL,
            strike_price=Decimal("1400"), lot_size=250, expiry_date=EXPIRY_DATE,
        ),
    ]
    session.add_all(contracts)

    # (contract_id, counterparty_id, buy_sell, quantity_lots, trade_price)
    positions = [
        (NIFTY_FUT, "BRK-001", BuySell.BUY, 10, Decimal("23800")),
        (NIFTY_FUT, "BRK-002", BuySell.SELL, 6, Decimal("23800")),
        (NIFTY_FUT, "BRK-003", BuySell.SELL, 4, Decimal("23800")),
        (NIFTY_CE, "BRK-001", BuySell.BUY, 8, Decimal("180")),
        (NIFTY_CE, "BRK-004", BuySell.SELL, 8, Decimal("180")),
        (RELIANCE_FUT, "BRK-002", BuySell.BUY, 5, Decimal("1380")),
        (RELIANCE_FUT, "BRK-005", BuySell.SELL, 5, Decimal("1380")),
        (RELIANCE_CE, "BRK-002", BuySell.BUY, 3, Decimal("45")),
        (RELIANCE_CE, "BRK-006", BuySell.SELL, 3, Decimal("45")),
    ]
    for i, (contract_id, cp_id, side, qty, price) in enumerate(positions):
        session.add(DerivativePosition(
            position_id=f"DPOS-{i:03d}",
            contract_id=contract_id,
            counterparty_id=cp_id,
            buy_sell=side,
            quantity=qty,
            trade_price=price,
            position_date=position_date,
        ))
    session.commit()


def seed_collateral_records(session: Session, as_of_date: date) -> None:
    """Collateral pledged by a few CMs, spanning cash, G-Sec, and equity types."""
    records = [
        ("BRK-001", CollateralType.CASH, Decimal("30000000"), 0.0),
        ("BRK-001", CollateralType.GOVERNMENT_SECURITY, Decimal("8000000"), 5.0),
        ("BRK-001", CollateralType.EQUITY, Decimal("5000000"), 30.0),
        ("BRK-002", CollateralType.CASH, Decimal("45000000"), 0.0),
        ("BRK-002", CollateralType.BANK_GUARANTEE, Decimal("10000000"), 0.0),
        ("BRK-002", CollateralType.EQUITY, Decimal("20000000"), 30.0),
    ]
    for i, (cp_id, ctype, value, haircut) in enumerate(records):
        session.add(CollateralRecord(
            collateral_id=f"COLL-{i:03d}",
            counterparty_id=cp_id,
            collateral_type=ctype,
            value=value,
            haircut_pct=haircut,
            as_of_date=as_of_date,
        ))
    session.commit()


def seed_debt_instruments_and_trades(session: Session, trade_date: date, settlement_date: date) -> None:
    """One corporate bond and one G-Sec, each with a couple of DvP-I trades."""
    session.add_all([
        DebtInstrument(
            isin=CORP_BOND_ISIN, issuer="Reliance Industries Ltd", instrument_type=DebtInstrumentType.CORPORATE_BOND,
            face_value=Decimal("1000"), coupon_rate_pct=8.0, coupon_frequency=2,
            issue_date=date(2024, 1, 15), maturity_date=date(2029, 1, 15),
            day_count_convention=DayCountConvention.THIRTY_360,
        ),
        DebtInstrument(
            isin=GSEC_ISIN, issuer="Government of India", instrument_type=DebtInstrumentType.GSEC,
            face_value=Decimal("100"), coupon_rate_pct=7.1, coupon_frequency=2,
            issue_date=date(2023, 4, 1), maturity_date=date(2033, 4, 1),
            day_count_convention=DayCountConvention.ACTUAL_ACTUAL,
        ),
    ])

    trades = [
        ("DEBT-T001", CORP_BOND_ISIN, "BRK-001", "BRK-002", 1000, Decimal("101.25"), ProductSegment.DEBT_CORP_BOND, "CBRICS"),
        ("DEBT-T002", CORP_BOND_ISIN, "BRK-003", "BRK-004", 500, Decimal("100.80"), ProductSegment.DEBT_CORP_BOND, "RFQ"),
        ("DEBT-T003", GSEC_ISIN, "BRK-002", "BRK-005", 2000, Decimal("99.50"), ProductSegment.DEBT_GSEC, "CCIL"),
    ]
    for trade_id, isin, buyer, seller, qty, price, segment, source in trades:
        session.add(DebtTrade(
            trade_id=trade_id, isin=isin, buyer_id=buyer, seller_id=seller,
            quantity=qty, clean_price=price, trade_date=trade_date, settlement_date=settlement_date,
            product_segment=segment, source=source, status=DebtTradeStatus.PENDING,
        ))
    session.commit()


def seed_t0_equity_trades(session: Session, settle_date: date) -> None:
    """A few same-day-settling equity cash trades, distinct from the main T+1 ledger."""
    trades = [
        ("T0-001", "INE040A01034", 200, Decimal("1650.50"), BuySell.BUY, "BRK-001"),
        ("T0-002", "INE040A01034", 80, Decimal("1650.50"), BuySell.SELL, "BRK-001"),
        ("T0-003", "INE062A01020", 150, Decimal("455.25"), BuySell.BUY, "BRK-002"),
    ]
    for trade_id, isin, qty, price, side, cp_id in trades:
        session.add(Trade(
            trade_id=trade_id, isin=isin, security_name=isin, quantity=qty, price=price,
            trade_date=settle_date, settlement_date=settle_date, settlement_cycle=SettlementCycle.T0,
            counterparty_id=cp_id, counterparty_type=CounterpartyType.BROKER, exchange=Exchange.NSE,
            buy_sell=side, source_system=SourceSystem.OMS, product_segment=ProductSegment.EQUITY_CASH,
        ))
    session.commit()
