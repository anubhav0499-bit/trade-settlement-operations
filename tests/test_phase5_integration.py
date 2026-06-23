"""End-to-end integration test across Phase 5 advanced features.

Exercises a single shared in-memory DB through: clearing member hierarchy
registration, T+0 same-day obligation netting, multi-CM obligation
aggregation, portfolio stress ranking, and the SGF default waterfall —
verifying the modules compose correctly rather than just in isolation.
"""

import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, DerivativePosition, Trade
from src.models.enums import (
    BuySell,
    CMType,
    CounterpartyType,
    Exchange,
    ProductSegment,
    SettlementCycle,
    SourceSystem,
)
from src.cm_hierarchy.hierarchy import aggregate_obligations, register_clearing_member
from src.derivatives.bond_futures import DeliverableBond, identify_cheapest_to_deliver
from src.risk.stress_test import rank_top_n_stressed_cms
from src.settlement.t0_engine import compute_t0_obligations
from src.sgf.waterfall import WaterfallInputs, get_waterfall_summary, run_default_waterfall


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_t0_settlement_and_cm_aggregation_compose(db_session):
    register_clearing_member(db_session, "TM-CM-1", "Parent CM", CMType.TM_CM, Decimal("30000000"), Decimal("5000000"))
    register_clearing_member(db_session, "SUB-1", "Sub TM", CMType.TM_CM, Decimal("1000000"), Decimal("0"), parent_cm_id="TM-CM-1")

    settle_date = date(2026, 6, 25)
    db_session.add(Trade(
        trade_id="T1", isin="INE001A01036", security_name="TestCo", quantity=100,
        price=Decimal("50"), trade_date=settle_date, settlement_date=settle_date,
        settlement_cycle=SettlementCycle.T0, counterparty_id="TM-CM-1",
        counterparty_type=CounterpartyType.BROKER, exchange=Exchange.NSE,
        buy_sell=BuySell.BUY, source_system=SourceSystem.OMS,
        product_segment=ProductSegment.EQUITY_CASH,
    ))
    db_session.add(Trade(
        trade_id="T2", isin="INE001A01036", security_name="TestCo", quantity=20,
        price=Decimal("50"), trade_date=settle_date, settlement_date=settle_date,
        settlement_cycle=SettlementCycle.T0, counterparty_id="SUB-1",
        counterparty_type=CounterpartyType.BROKER, exchange=Exchange.NSE,
        buy_sell=BuySell.BUY, source_system=SourceSystem.OMS,
        product_segment=ProductSegment.EQUITY_CASH,
    ))
    db_session.commit()

    obligations = compute_t0_obligations(db_session)
    assert len(obligations) == 2

    agg = aggregate_obligations(db_session, "TM-CM-1", settle_date)
    assert agg["obligation_count"] == 2
    assert agg["total_value"] == Decimal("5000") + Decimal("1000")


def test_stress_shortfall_feeds_sgf_waterfall(db_session):
    settle_date = date(2026, 6, 25)
    db_session.add(DerivativePosition(
        position_id="P1", contract_id="C1", counterparty_id="CM-1",
        buy_sell=BuySell.BUY, quantity=10000, trade_price=Decimal("100"),
        position_date=settle_date,
    ))
    db_session.commit()

    reference_prices = {"C1": Decimal("100")}
    margin_held = {"CM-1": Decimal("50000")}
    results = rank_top_n_stressed_cms(
        db_session, ["CM-1"], settle_date, Decimal("15"), reference_prices, margin_held, top_n=1
    )
    shortfall = results[0].shortfall
    assert shortfall > 0

    waterfall_inputs = WaterfallInputs(
        defaulter_margin_collateral=Decimal("50000"),
        defaulter_base_capital=Decimal("20000"),
        defaulter_sgf_contribution=Decimal("10000"),
        nse_sgf_contribution=Decimal("15000"),
        other_cm_sgf_contributions={"CM-2": Decimal("30000")},
        nse_other_resources=Decimal("10000"),
        insurance_cover=Decimal("0"),
    )
    steps = run_default_waterfall(shortfall, waterfall_inputs)
    summary = get_waterfall_summary(steps)
    assert summary["total_shortfall"] == shortfall


def test_ird_ctd_selection_is_deterministic():
    bonds = [
        DeliverableBond("BOND-A", Decimal("6"), Decimal("10"), Decimal("100")),
        DeliverableBond("BOND-B", Decimal("8"), Decimal("10"), Decimal("118")),
        DeliverableBond("BOND-C", Decimal("7"), Decimal("10"), Decimal("109")),
    ]
    ctd_1 = identify_cheapest_to_deliver(bonds, Decimal("100"), Decimal("6"))
    ctd_2 = identify_cheapest_to_deliver(bonds, Decimal("100"), Decimal("6"))
    assert ctd_1 == ctd_2
