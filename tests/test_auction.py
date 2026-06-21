"""Unit tests for the auction & close-out sub-workflow."""

import json
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, Obligation, AuctionRecord
from src.models.enums import (
    AuctionOutcome,
    AuctionStatus,
    ConfirmationStatus,
    CounterpartyType,
    Exchange,
    MatchStatus,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    SettlementCycle,
)
from src.auction.close_out import (
    detect_short_deliveries,
    execute_auction,
    initiate_auction,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _make_instructed_sell_obligation(session, settle_date=date(2026, 6, 2)):
    ob = Obligation(
        obligation_id=str(uuid.uuid4()),
        isin="INE002A01018",
        security_name="RELIANCE",
        net_quantity=100,
        net_direction=NetDirection.PAY_IN,  # seller delivers
        vwap_price=Decimal("2900.0000"),
        net_value=Decimal("290000.00"),
        settlement_date=settle_date,
        settlement_cycle=SettlementCycle.T1,
        counterparty_id="BRK-001",
        counterparty_type=CounterpartyType.BROKER,
        exchange=Exchange.NSE,
        obligation_stage=ObligationStage.FINAL,
        status=ObligationStatus.INSTRUCTED,
        match_status=MatchStatus.MATCHED,
        confirmation_status=ConfirmationStatus.NOT_REQUIRED,
        computed_at=datetime.utcnow(),
        source_trade_ids=json.dumps(["TRD-00001"]),
    )
    session.add(ob)
    session.commit()
    return ob


class TestDetectShortDeliveries:
    def test_instructed_sell_past_settlement(self, session):
        ob = _make_instructed_sell_obligation(session, settle_date=date(2026, 6, 2))
        shorts = detect_short_deliveries(session, [ob], date(2026, 6, 2))
        assert len(shorts) == 1
        assert ob.status == ObligationStatus.FAILED

    def test_buy_side_not_flagged(self, session):
        ob = _make_instructed_sell_obligation(session)
        ob.net_direction = NetDirection.PAY_OUT  # buyer
        session.commit()

        shorts = detect_short_deliveries(session, [ob], date(2026, 6, 2))
        assert len(shorts) == 0

    def test_future_settlement_not_flagged(self, session):
        ob = _make_instructed_sell_obligation(session, settle_date=date(2026, 6, 5))
        shorts = detect_short_deliveries(session, [ob], date(2026, 6, 2))
        assert len(shorts) == 0


class TestInitiateAuction:
    def test_creates_auction_record(self, session):
        ob = _make_instructed_sell_obligation(session)
        ob.status = ObligationStatus.FAILED
        session.commit()

        auction = initiate_auction(
            session, ob,
            valuation_price=Decimal("2880.0000"),
            auction_date=date(2026, 6, 3),
        )
        assert auction.short_quantity == 100
        assert auction.valuation_price == Decimal("2880.0000")
        assert auction.status == AuctionStatus.INITIATED
        assert ob.status == ObligationStatus.AUCTION
        # Auction settles T+2 (next business day after auction)
        assert auction.auction_settlement_date == date(2026, 6, 4)


class TestExecuteAuction:
    def test_auction_success_no_penalty(self, session):
        ob = _make_instructed_sell_obligation(session)
        auction = initiate_auction(
            session, ob,
            valuation_price=Decimal("2900.0000"),
            auction_date=date(2026, 6, 3),
        )

        execute_auction(
            session, auction,
            auction_price=Decimal("2850.0000"),  # below valuation
            closing_price_auction_day=Decimal("2880.0000"),
            highest_price_trade_to_auction=Decimal("2920.0000"),
        )
        assert auction.outcome == AuctionOutcome.AUCTION_SUCCESS
        assert auction.penalty_amount == Decimal("0")

    def test_auction_success_with_penalty(self, session):
        ob = _make_instructed_sell_obligation(session)
        auction = initiate_auction(
            session, ob,
            valuation_price=Decimal("2900.0000"),
            auction_date=date(2026, 6, 3),
        )

        execute_auction(
            session, auction,
            auction_price=Decimal("2950.0000"),  # 50 above valuation
            closing_price_auction_day=Decimal("2930.0000"),
            highest_price_trade_to_auction=Decimal("2960.0000"),
        )
        assert auction.outcome == AuctionOutcome.AUCTION_SUCCESS
        # Penalty = (2950 - 2900) × 100 = 5000
        assert auction.penalty_amount == Decimal("5000.00")

    def test_close_out_when_auction_fails(self, session):
        ob = _make_instructed_sell_obligation(session)
        auction = initiate_auction(
            session, ob,
            valuation_price=Decimal("2900.0000"),
            auction_date=date(2026, 6, 3),
        )

        execute_auction(
            session, auction,
            auction_price=None,  # auction failed
            closing_price_auction_day=Decimal("2880.0000"),
            highest_price_trade_to_auction=Decimal("2920.0000"),
        )
        assert auction.outcome == AuctionOutcome.CLOSED_OUT
        # Close-out price = max(2920, 2880 * 1.20) = max(2920, 3456) = 3456
        assert auction.close_out_price == Decimal("3456.0000")
        # Penalty = (3456 - 2900) × 100 = 55600
        assert auction.penalty_amount == Decimal("55600.00")

    def test_close_out_highest_price_wins(self, session):
        ob = _make_instructed_sell_obligation(session)
        auction = initiate_auction(
            session, ob,
            valuation_price=Decimal("2900.0000"),
            auction_date=date(2026, 6, 3),
        )

        execute_auction(
            session, auction,
            auction_price=None,
            closing_price_auction_day=Decimal("2400.0000"),  # low close
            highest_price_trade_to_auction=Decimal("3500.0000"),  # high peak
        )
        assert auction.outcome == AuctionOutcome.CLOSED_OUT
        # max(3500, 2400*1.20=2880) = 3500
        assert auction.close_out_price == Decimal("3500.0000")
