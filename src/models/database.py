import json

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.models.enums import (
    AuctionOutcome,
    AuctionStatus,
    BreakStatus,
    BreakType,
    BuySell,
    CMType,
    CollateralType,
    ConfirmationStatus,
    ContractType,
    CounterpartyType,
    DayCountConvention,
    DebtInstrumentType,
    DebtTradeStatus,
    DeliveryType,
    Depository,
    Exchange,
    InstructionDirection,
    InstructionStatus,
    MarginType,
    MatchStatus,
    NetDirection,
    ObligationStage,
    ObligationStatus,
    OptionType,
    ProductSegment,
    Segment,
    SettlementCycle,
    Severity,
    SourceSystem,
)
from src.utils.clock import utcnow


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    trade_id = Column(String, primary_key=True)
    isin = Column(String, nullable=False, index=True)
    security_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Numeric(12, 4), nullable=False)
    trade_date = Column(Date, nullable=False, index=True)
    settlement_date = Column(Date, nullable=False, index=True)
    settlement_cycle = Column(SAEnum(SettlementCycle), nullable=False)
    counterparty_id = Column(String, nullable=False, index=True)
    counterparty_type = Column(SAEnum(CounterpartyType), nullable=False)
    exchange = Column(SAEnum(Exchange), nullable=False)
    buy_sell = Column(SAEnum(BuySell), nullable=False)
    currency = Column(String, nullable=False, default="INR")
    source_system = Column(SAEnum(SourceSystem), nullable=False, index=True)
    segment = Column(SAEnum(Segment), nullable=False, default=Segment.NORMAL)
    product_segment = Column(
        SAEnum(ProductSegment), nullable=False, default=ProductSegment.EQUITY_CASH
    )
    created_at = Column(DateTime, nullable=False, default=utcnow)


class Obligation(Base):
    __tablename__ = "obligations"

    obligation_id = Column(String, primary_key=True)
    isin = Column(String, nullable=False, index=True)
    security_name = Column(String, nullable=False)
    net_quantity = Column(Integer, nullable=False)
    net_direction = Column(SAEnum(NetDirection), nullable=False)
    vwap_price = Column(Numeric(12, 4), nullable=False)
    net_value = Column(Numeric(15, 2), nullable=False)
    settlement_date = Column(Date, nullable=False, index=True)
    settlement_cycle = Column(SAEnum(SettlementCycle), nullable=False)
    counterparty_id = Column(String, nullable=False, index=True)
    counterparty_type = Column(SAEnum(CounterpartyType), nullable=False)
    exchange = Column(SAEnum(Exchange), nullable=False)
    obligation_stage = Column(SAEnum(ObligationStage), nullable=False)
    product_segment = Column(
        SAEnum(ProductSegment), nullable=False, default=ProductSegment.EQUITY_CASH
    )
    status = Column(
        SAEnum(ObligationStatus), nullable=False, default=ObligationStatus.PENDING
    )
    match_status = Column(
        SAEnum(MatchStatus), nullable=False, default=MatchStatus.UNMATCHED
    )
    confirmation_status = Column(
        SAEnum(ConfirmationStatus),
        nullable=False,
        default=ConfirmationStatus.NOT_REQUIRED,
    )
    computed_at = Column(DateTime, nullable=False, default=utcnow)
    source_trade_ids = Column(Text, nullable=False)  # JSON list
    instruction_id = Column(String, nullable=True)

    def get_source_trade_ids(self) -> list[str]:
        return json.loads(self.source_trade_ids)

    def set_source_trade_ids(self, trade_ids: list[str]):
        self.source_trade_ids = json.dumps(trade_ids)


class SSIRecord(Base):
    __tablename__ = "ssi_golden_copy"

    ssi_id = Column(String, primary_key=True)
    counterparty_id = Column(String, nullable=False, index=True)
    settlement_bank = Column(String, nullable=False)
    bank_account = Column(String, nullable=False)
    dp_id = Column(String, nullable=False)
    dp_account = Column(String, nullable=False)
    depository = Column(SAEnum(Depository), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)


class BreakRecord(Base):
    __tablename__ = "breaks"

    break_id = Column(String, primary_key=True)
    obligation_id = Column(String, nullable=False, index=True)
    break_type = Column(SAEnum(BreakType), nullable=False)
    severity = Column(SAEnum(Severity), nullable=False)
    value_at_risk = Column(Numeric(15, 2), nullable=True)
    age_hours = Column(Float, nullable=True)
    age_days = Column(Integer, nullable=True)
    status = Column(SAEnum(BreakStatus), nullable=False, default=BreakStatus.OPEN)
    recommended_action = Column(Text, nullable=True)
    resolution_notes = Column(Text, nullable=True)
    resolved_by = Column(String, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    escalation_level = Column(Integer, nullable=False, default=0)


class SettlementInstruction(Base):
    __tablename__ = "settlement_instructions"

    instruction_id = Column(String, primary_key=True)
    obligation_id = Column(String, nullable=False, index=True)
    isin = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    settlement_value = Column(Numeric(15, 2), nullable=False)
    direction = Column(SAEnum(InstructionDirection), nullable=False)
    dp_id = Column(String, nullable=False)
    dp_account = Column(String, nullable=False)
    settlement_bank = Column(String, nullable=False)
    bank_account = Column(String, nullable=False)
    depository = Column(SAEnum(Depository), nullable=False)
    status = Column(
        SAEnum(InstructionStatus),
        nullable=False,
        default=InstructionStatus.GENERATED,
    )
    generated_at = Column(DateTime, nullable=False, default=utcnow)


class AuctionRecord(Base):
    __tablename__ = "auctions"

    auction_id = Column(String, primary_key=True)
    obligation_id = Column(String, nullable=False, index=True)
    isin = Column(String, nullable=False)
    short_quantity = Column(Integer, nullable=False)
    valuation_price = Column(Numeric(12, 4), nullable=False)
    auction_price = Column(Numeric(12, 4), nullable=True)
    auction_date = Column(Date, nullable=False)
    auction_settlement_date = Column(Date, nullable=False)
    close_out_price = Column(Numeric(12, 4), nullable=True)
    penalty_amount = Column(Numeric(15, 2), nullable=False, default=0)
    outcome = Column(SAEnum(AuctionOutcome), nullable=True)
    status = Column(
        SAEnum(AuctionStatus), nullable=False, default=AuctionStatus.INITIATED
    )
    created_at = Column(DateTime, nullable=False, default=utcnow)


class Counterparty(Base):
    __tablename__ = "counterparties"

    counterparty_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    counterparty_type = Column(SAEnum(CounterpartyType), nullable=False)
    exchange_membership = Column(String, nullable=False)  # NSE / BSE / BOTH
    is_active = Column(Boolean, nullable=False, default=True)


class CustodianHolding(Base):
    __tablename__ = "custodian_holdings"

    holding_id = Column(String, primary_key=True)
    counterparty_id = Column(String, nullable=False, index=True)
    isin = Column(String, nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    statement_date = Column(Date, nullable=False, index=True)
    source = Column(
        String, nullable=False, default="CUSTODIAN_EOD_STATEMENT"
    )


class AgenticAuditLog(Base):
    __tablename__ = "agentic_audit_log"

    log_id = Column(String, primary_key=True)
    obligation_id = Column(String, nullable=True, index=True)
    break_id = Column(String, nullable=True, index=True)
    node_name = Column(String, nullable=False)
    inputs = Column(Text, nullable=False)  # JSON
    conclusion = Column(Text, nullable=False)
    rationale = Column(Text, nullable=False)
    timestamp = Column(DateTime, nullable=False, default=utcnow)


class PositionRecord(Base):
    """Derived position from settled obligations, used for EOD recon."""

    __tablename__ = "positions"

    position_id = Column(String, primary_key=True)
    counterparty_id = Column(String, nullable=False, index=True)
    isin = Column(String, nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    as_of_date = Column(Date, nullable=False, index=True)
    last_updated = Column(DateTime, nullable=False, default=utcnow)


class DerivativeContract(Base):
    """A listed F&O / currency / interest rate derivative contract."""

    __tablename__ = "derivative_contracts"

    contract_id = Column(String, primary_key=True)
    underlying = Column(String, nullable=False, index=True)
    product_segment = Column(SAEnum(ProductSegment), nullable=False)
    contract_type = Column(SAEnum(ContractType), nullable=False)
    option_type = Column(SAEnum(OptionType), nullable=True)
    delivery_type = Column(SAEnum(DeliveryType), nullable=False)
    strike_price = Column(Numeric(12, 4), nullable=True)
    lot_size = Column(Integer, nullable=False)
    expiry_date = Column(Date, nullable=False, index=True)
    currency = Column(String, nullable=False, default="INR")
    created_at = Column(DateTime, nullable=False, default=utcnow)


class DerivativePosition(Base):
    """An open position held by a counterparty in a derivative contract."""

    __tablename__ = "derivative_positions"

    position_id = Column(String, primary_key=True)
    contract_id = Column(String, nullable=False, index=True)
    counterparty_id = Column(String, nullable=False, index=True)
    buy_sell = Column(SAEnum(BuySell), nullable=False)
    quantity = Column(Integer, nullable=False)
    trade_price = Column(Numeric(12, 4), nullable=False)
    position_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class MTMSettlement(Base):
    """Daily mark-to-market settlement on an open derivative position."""

    __tablename__ = "mtm_settlements"

    mtm_id = Column(String, primary_key=True)
    contract_id = Column(String, nullable=False, index=True)
    counterparty_id = Column(String, nullable=False, index=True)
    settlement_date = Column(Date, nullable=False, index=True)
    settlement_price = Column(Numeric(12, 4), nullable=False)
    mtm_amount = Column(Numeric(15, 2), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class MarginRecord(Base):
    """A margin requirement levied on a counterparty for a product segment."""

    __tablename__ = "margin_records"

    margin_id = Column(String, primary_key=True)
    counterparty_id = Column(String, nullable=False, index=True)
    product_segment = Column(SAEnum(ProductSegment), nullable=False)
    margin_type = Column(SAEnum(MarginType), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    as_of_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class CollateralRecord(Base):
    """Collateral pledged by a counterparty against margin requirements."""

    __tablename__ = "collateral_records"

    collateral_id = Column(String, primary_key=True)
    counterparty_id = Column(String, nullable=False, index=True)
    collateral_type = Column(SAEnum(CollateralType), nullable=False)
    value = Column(Numeric(15, 2), nullable=False)
    haircut_pct = Column(Float, nullable=False, default=0.0)
    as_of_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class DebtInstrument(Base):
    """A corporate bond or G-Sec reference record."""

    __tablename__ = "debt_instruments"

    isin = Column(String, primary_key=True)
    issuer = Column(String, nullable=False)
    instrument_type = Column(SAEnum(DebtInstrumentType), nullable=False)
    face_value = Column(Numeric(15, 2), nullable=False)
    coupon_rate_pct = Column(Float, nullable=False)
    coupon_frequency = Column(Integer, nullable=False, default=2)
    issue_date = Column(Date, nullable=False)
    maturity_date = Column(Date, nullable=False, index=True)
    day_count_convention = Column(SAEnum(DayCountConvention), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class DebtTrade(Base):
    """A corporate bond / G-Sec trade settling DvP-I (gross, no netting)."""

    __tablename__ = "debt_trades"

    trade_id = Column(String, primary_key=True)
    isin = Column(String, nullable=False, index=True)
    buyer_id = Column(String, nullable=False, index=True)
    seller_id = Column(String, nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    clean_price = Column(Numeric(12, 4), nullable=False)
    trade_date = Column(Date, nullable=False, index=True)
    settlement_date = Column(Date, nullable=False, index=True)
    product_segment = Column(SAEnum(ProductSegment), nullable=False)
    source = Column(String, nullable=False)
    status = Column(
        SAEnum(DebtTradeStatus), nullable=False, default=DebtTradeStatus.PENDING
    )
    securities_received = Column(Boolean, nullable=False, default=False)
    funds_received = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class ClearingMember(Base):
    """A clearing member in NSE Clearing's hierarchy (TM-CM, SCM, or PCM).

    parent_cm_id is set when this member is a sub-TM cleared by a TM-CM or PCM,
    enabling obligation aggregation up the hierarchy.
    """

    __tablename__ = "clearing_members"

    cm_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    cm_type = Column(SAEnum(CMType), nullable=False)
    parent_cm_id = Column(String, nullable=True, index=True)
    net_worth = Column(Numeric(15, 2), nullable=False)
    security_deposit = Column(Numeric(15, 2), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


def get_engine(db_path: str = "data/generated/settlement.db"):
    return create_engine(f"sqlite:///{db_path}", echo=False)


def create_tables(engine):
    Base.metadata.create_all(engine)


def get_session(engine) -> Session:
    return sessionmaker(bind=engine)()
