"""Unit tests for the netting & obligation engine."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Trade
from src.models.enums import (
    BuySell,
    CounterpartyType,
    Exchange,
    NetDirection,
    ObligationStage,
    Segment,
    SettlementCycle,
    SourceSystem,
)
from src.netting.obligation_engine import (
    _compute_vwap,
    _net_trades,
    compute_obligations,
)
from src.utils.clock import utcnow


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _make_trade(
    trade_id=None,
    isin="INE002A01018",
    qty=100,
    price=Decimal("2900.0000"),
    trade_date=date(2026, 6, 1),
    settle_date=date(2026, 6, 2),
    buy_sell=BuySell.BUY,
    cp_id="BRK-001",
    exchange=Exchange.NSE,
    source=SourceSystem.OMS,
) -> Trade:
    return Trade(
        trade_id=trade_id or f"TRD-{uuid.uuid4().hex[:8]}",
        isin=isin,
        security_name="RELIANCE INDUSTRIES",
        quantity=qty,
        price=price,
        trade_date=trade_date,
        settlement_date=settle_date,
        settlement_cycle=SettlementCycle.T1,
        counterparty_id=cp_id,
        counterparty_type=CounterpartyType.BROKER,
        exchange=exchange,
        buy_sell=buy_sell,
        currency="INR",
        source_system=source,
        segment=Segment.NORMAL,
        created_at=utcnow(),
    )


class TestVWAP:
    def test_single_trade(self):
        t = _make_trade(qty=100, price=Decimal("2900.0000"))
        assert _compute_vwap([t]) == Decimal("2900.0000")

    def test_multiple_trades_equal_qty(self):
        t1 = _make_trade(qty=100, price=Decimal("100.0000"))
        t2 = _make_trade(qty=100, price=Decimal("200.0000"))
        assert _compute_vwap([t1, t2]) == Decimal("150.0000")

    def test_volume_weighted(self):
        t1 = _make_trade(qty=300, price=Decimal("100.0000"))
        t2 = _make_trade(qty=100, price=Decimal("200.0000"))
        # VWAP = (300*100 + 100*200) / 400 = 50000/400 = 125
        assert _compute_vwap([t1, t2]) == Decimal("125.0000")


class TestNetTrades:
    def test_all_buys(self):
        trades = [
            _make_trade(qty=100, buy_sell=BuySell.BUY),
            _make_trade(qty=50, buy_sell=BuySell.BUY),
        ]
        qty, direction, ids = _net_trades(trades)
        assert qty == 150
        assert direction == NetDirection.PAY_OUT

    def test_all_sells(self):
        trades = [
            _make_trade(qty=100, buy_sell=BuySell.SELL),
            _make_trade(qty=50, buy_sell=BuySell.SELL),
        ]
        qty, direction, ids = _net_trades(trades)
        assert qty == 150
        assert direction == NetDirection.PAY_IN

    def test_net_to_buy(self):
        trades = [
            _make_trade(qty=200, buy_sell=BuySell.BUY),
            _make_trade(qty=50, buy_sell=BuySell.SELL),
        ]
        qty, direction, ids = _net_trades(trades)
        assert qty == 150
        assert direction == NetDirection.PAY_OUT

    def test_net_to_sell(self):
        trades = [
            _make_trade(qty=50, buy_sell=BuySell.BUY),
            _make_trade(qty=200, buy_sell=BuySell.SELL),
        ]
        qty, direction, ids = _net_trades(trades)
        assert qty == 150
        assert direction == NetDirection.PAY_IN

    def test_net_to_zero(self):
        trades = [
            _make_trade(qty=100, buy_sell=BuySell.BUY),
            _make_trade(qty=100, buy_sell=BuySell.SELL),
        ]
        qty, direction, ids = _net_trades(trades)
        assert qty == 0


class TestComputeObligations:
    def test_single_trade_produces_obligation(self, session):
        t = _make_trade(source=SourceSystem.OMS)
        session.add(t)
        session.commit()

        obligations = compute_obligations(
            session, SourceSystem.OMS, ObligationStage.FINAL
        )
        assert len(obligations) == 1
        ob = obligations[0]
        assert ob.isin == "INE002A01018"
        assert ob.net_quantity == 100
        assert ob.net_direction == NetDirection.PAY_OUT

    def test_netting_same_group(self, session):
        t1 = _make_trade(qty=200, buy_sell=BuySell.BUY, source=SourceSystem.OMS)
        t2 = _make_trade(qty=80, buy_sell=BuySell.SELL, source=SourceSystem.OMS)
        session.add_all([t1, t2])
        session.commit()

        obligations = compute_obligations(
            session, SourceSystem.OMS, ObligationStage.FINAL
        )
        assert len(obligations) == 1
        ob = obligations[0]
        assert ob.net_quantity == 120
        assert ob.net_direction == NetDirection.PAY_OUT

    def test_different_isins_separate_obligations(self, session):
        t1 = _make_trade(isin="INE002A01018", source=SourceSystem.OMS)
        t2 = _make_trade(isin="INE009A01021", source=SourceSystem.OMS)
        session.add_all([t1, t2])
        session.commit()

        obligations = compute_obligations(
            session, SourceSystem.OMS, ObligationStage.FINAL
        )
        assert len(obligations) == 2
        isins = {ob.isin for ob in obligations}
        assert isins == {"INE002A01018", "INE009A01021"}

    def test_zero_net_excluded(self, session):
        t1 = _make_trade(qty=100, buy_sell=BuySell.BUY, source=SourceSystem.OMS)
        t2 = _make_trade(qty=100, buy_sell=BuySell.SELL, source=SourceSystem.OMS)
        session.add_all([t1, t2])
        session.commit()

        obligations = compute_obligations(
            session, SourceSystem.OMS, ObligationStage.FINAL
        )
        assert len(obligations) == 0
