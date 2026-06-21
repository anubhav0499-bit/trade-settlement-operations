"""
Auction & Close-Out Sub-Workflow (§8).

Handles short delivery resolution per NSE/BSE rules:
1. Sell-side obligation not delivered by settlement day → flag as short delivery
2. Compute valuation debit using closing price on day preceding pay-in
3. Trigger buy-in auction on T+1 day within ±20% price band
4. Auction settles on T+2 (buyer doesn't receive until T+2)
5. If auction price > valuation price → charge defaulting member difference + penalty
6. If auction fails → close out at higher of: highest price from trade-to-auction
   date, or 20% above auction day closing price
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.models.database import AuctionRecord, Obligation
from src.models.enums import (
    AuctionOutcome,
    AuctionStatus,
    NetDirection,
    ObligationStatus,
)
from src.utils.config_loader import get_confirmation_config


def detect_short_deliveries(
    session: Session,
    obligations: list[Obligation],
    settlement_day: date,
) -> list[Obligation]:
    """Identify sell-side obligations that were not delivered by settlement day."""
    short = []
    for ob in obligations:
        if (
            ob.status == ObligationStatus.INSTRUCTED
            and ob.net_direction == NetDirection.PAY_IN  # seller delivers
            and ob.settlement_date <= settlement_day
        ):
            ob.status = ObligationStatus.FAILED
            short.append(ob)

    session.commit()
    return short


def initiate_auction(
    session: Session,
    obligation: Obligation,
    valuation_price: Decimal,
    auction_date: date,
    config: dict | None = None,
) -> AuctionRecord:
    """Initiate a buy-in auction for a short-delivered obligation."""
    if config is None:
        config = get_confirmation_config()

    auction_cfg = config["auction"]
    settlement_offset = auction_cfg["auction_settlement_offset"]
    auction_settlement_date = auction_date + timedelta(days=settlement_offset)

    # Skip weekends for settlement date
    while auction_settlement_date.weekday() >= 5:
        auction_settlement_date += timedelta(days=1)

    auction = AuctionRecord(
        auction_id=str(uuid.uuid4()),
        obligation_id=obligation.obligation_id,
        isin=obligation.isin,
        short_quantity=obligation.net_quantity,
        valuation_price=valuation_price,
        auction_date=auction_date,
        auction_settlement_date=auction_settlement_date,
        status=AuctionStatus.INITIATED,
    )

    obligation.status = ObligationStatus.AUCTION
    session.add(auction)
    session.commit()
    return auction


def execute_auction(
    session: Session,
    auction: AuctionRecord,
    auction_price: Decimal | None,
    closing_price_auction_day: Decimal,
    highest_price_trade_to_auction: Decimal,
    config: dict | None = None,
) -> AuctionRecord:
    """Execute auction or close-out.

    Args:
        auction: The auction record
        auction_price: Price obtained in auction (None if auction failed)
        closing_price_auction_day: Closing price on auction day
        highest_price_trade_to_auction: Highest price from trade date to auction date
        config: Auction config
    """
    if config is None:
        config = get_confirmation_config()

    auction_cfg = config["auction"]
    premium_pct = Decimal(str(auction_cfg["close_out_premium_pct"]))

    if auction_price is not None:
        # Auction succeeded
        auction.auction_price = auction_price
        auction.outcome = AuctionOutcome.AUCTION_SUCCESS
        auction.status = AuctionStatus.AUCTION_HELD

        # Penalty: if auction_price > valuation_price, charge the difference
        if auction_price > auction.valuation_price:
            diff_per_share = auction_price - auction.valuation_price
            penalty = (diff_per_share * auction.short_quantity).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            auction.penalty_amount = penalty
        else:
            auction.penalty_amount = Decimal("0")

        # Update obligation
        ob = session.query(Obligation).filter(
            Obligation.obligation_id == auction.obligation_id
        ).first()
        if ob:
            ob.status = ObligationStatus.AUCTION

    else:
        # Auction failed — close out
        close_out_floor = closing_price_auction_day * (
            Decimal("1") + premium_pct / Decimal("100")
        )
        close_out_price = max(highest_price_trade_to_auction, close_out_floor)
        close_out_price = close_out_price.quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )

        auction.close_out_price = close_out_price
        auction.outcome = AuctionOutcome.CLOSED_OUT
        auction.status = AuctionStatus.CLOSED_OUT

        # Penalty = (close_out_price - valuation_price) × quantity
        if close_out_price > auction.valuation_price:
            diff = close_out_price - auction.valuation_price
            auction.penalty_amount = (diff * auction.short_quantity).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            auction.penalty_amount = Decimal("0")

        ob = session.query(Obligation).filter(
            Obligation.obligation_id == auction.obligation_id
        ).first()
        if ob:
            ob.status = ObligationStatus.CLOSED_OUT

    session.commit()
    return auction


def settle_auction(session: Session, auction: AuctionRecord):
    """Mark an auction as settled (T+2 settlement)."""
    auction.status = AuctionStatus.SETTLED
    session.commit()
