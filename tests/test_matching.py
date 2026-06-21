"""Unit tests for the matching engine."""

import json
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Obligation
from src.models.enums import (
    CounterpartyType,
    Exchange,
    MatchStatus,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    SettlementCycle,
    ConfirmationStatus,
    BreakType,
)
from src.matching.engine import match_obligations, _price_within_tolerance


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _make_obligation(
    isin="INE002A01018",
    net_qty=100,
    direction=NetDirection.PAY_OUT,
    vwap=Decimal("2900.0000"),
    settle_date=date(2026, 6, 2),
    cp_id="BRK-001",
    cp_type=CounterpartyType.BROKER,
    exchange=Exchange.NSE,
    cycle=SettlementCycle.T1,
    source_prefix="",
) -> Obligation:
    trade_id = f"{source_prefix}TRD-{uuid.uuid4().hex[:6]}"
    return Obligation(
        obligation_id=str(uuid.uuid4()),
        isin=isin,
        security_name="RELIANCE INDUSTRIES",
        net_quantity=net_qty,
        net_direction=direction,
        vwap_price=vwap,
        net_value=vwap * net_qty,
        settlement_date=settle_date,
        settlement_cycle=cycle,
        counterparty_id=cp_id,
        counterparty_type=cp_type,
        exchange=exchange,
        obligation_stage=ObligationStage.FINAL,
        status=ObligationStatus.SSI_VALIDATED,
        match_status=MatchStatus.UNMATCHED,
        confirmation_status=ConfirmationStatus.NOT_REQUIRED,
        computed_at=datetime.utcnow(),
        source_trade_ids=json.dumps([trade_id]),
    )


class TestPriceTolerance:
    def test_exact_match(self):
        assert _price_within_tolerance(Decimal("100"), Decimal("100"), 0.5)

    def test_within_tolerance(self):
        assert _price_within_tolerance(Decimal("100"), Decimal("100.40"), 0.5)

    def test_at_boundary(self):
        assert _price_within_tolerance(Decimal("100"), Decimal("100.50"), 0.5)

    def test_beyond_tolerance(self):
        assert not _price_within_tolerance(Decimal("100"), Decimal("100.60"), 0.5)

    def test_zero_prices(self):
        assert _price_within_tolerance(Decimal("0"), Decimal("0"), 0.5)

    def test_one_zero(self):
        assert not _price_within_tolerance(Decimal("0"), Decimal("100"), 0.5)


class TestMatchObligations:
    def test_exact_match(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        internal = [_make_obligation()]
        counterparty = [_make_obligation(
            net_qty=internal[0].net_quantity,
            vwap=internal[0].vwap_price,
            cp_id=internal[0].counterparty_id,
            settle_date=internal[0].settlement_date,
        )]
        results = match_obligations(internal, counterparty, config)
        assert len(results) == 1
        assert results[0].status == MatchStatus.MATCHED

    def test_price_within_tolerance_matches(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        internal = [_make_obligation(vwap=Decimal("1000.0000"))]
        counterparty = [_make_obligation(
            net_qty=internal[0].net_quantity,
            vwap=Decimal("1004.0000"),  # 0.4% diff — within tolerance
            cp_id=internal[0].counterparty_id,
            settle_date=internal[0].settlement_date,
        )]
        results = match_obligations(internal, counterparty, config)
        assert results[0].status == MatchStatus.MATCHED

    def test_price_break(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        internal = [_make_obligation(vwap=Decimal("1000.0000"))]
        counterparty = [_make_obligation(
            net_qty=internal[0].net_quantity,
            vwap=Decimal("1020.0000"),  # 2% diff — outside tolerance
            cp_id=internal[0].counterparty_id,
            settle_date=internal[0].settlement_date,
        )]
        results = match_obligations(internal, counterparty, config)
        assert results[0].status == MatchStatus.BREAK
        assert results[0].break_type == BreakType.PRICE_MISMATCH

    def test_quantity_break(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        internal = [_make_obligation(net_qty=100)]
        counterparty = [_make_obligation(
            net_qty=90,
            vwap=internal[0].vwap_price,
            cp_id=internal[0].counterparty_id,
            settle_date=internal[0].settlement_date,
        )]
        results = match_obligations(internal, counterparty, config)
        assert results[0].status == MatchStatus.BREAK
        assert results[0].break_type == BreakType.QUANTITY_MISMATCH

    def test_unmatched_no_counterpart(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        internal = [_make_obligation()]
        counterparty = []
        results = match_obligations(internal, counterparty, config)
        assert results[0].status == MatchStatus.UNMATCHED

    def test_multiple_obligations_match_correctly(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        ob1 = _make_obligation(isin="INE002A01018", net_qty=100, cp_id="BRK-001")
        ob2 = _make_obligation(isin="INE009A01021", net_qty=200, cp_id="BRK-002")
        cp1 = _make_obligation(
            isin="INE002A01018", net_qty=100,
            vwap=ob1.vwap_price, cp_id="BRK-001",
            settle_date=ob1.settlement_date,
        )
        cp2 = _make_obligation(
            isin="INE009A01021", net_qty=200,
            vwap=ob2.vwap_price, cp_id="BRK-002",
            settle_date=ob2.settlement_date,
        )
        results = match_obligations([ob1, ob2], [cp1, cp2], config)
        assert all(r.status == MatchStatus.MATCHED for r in results)

    def test_direction_mismatch_breaks(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        internal = [_make_obligation(direction=NetDirection.PAY_OUT)]
        counterparty = [_make_obligation(
            net_qty=internal[0].net_quantity,
            vwap=internal[0].vwap_price,
            direction=NetDirection.PAY_IN,
            cp_id=internal[0].counterparty_id,
            settle_date=internal[0].settlement_date,
        )]
        results = match_obligations(internal, counterparty, config)
        assert results[0].status == MatchStatus.BREAK

    def test_different_isin_no_match(self):
        config = {"price_tolerance_pct": 0.5, "quantity_tolerance_abs": 0}
        internal = [_make_obligation(isin="INE002A01018")]
        counterparty = [_make_obligation(
            isin="INE009A01021",
            net_qty=internal[0].net_quantity,
            vwap=internal[0].vwap_price,
            cp_id=internal[0].counterparty_id,
            settle_date=internal[0].settlement_date,
        )]
        results = match_obligations(internal, counterparty, config)
        assert results[0].status == MatchStatus.UNMATCHED
