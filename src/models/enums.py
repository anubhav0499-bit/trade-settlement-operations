from enum import Enum


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class BuySell(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SettlementCycle(str, Enum):
    T0 = "T0"
    T1 = "T1"


class CounterpartyType(str, Enum):
    BROKER = "BROKER"
    CUSTODIAN = "CUSTODIAN"
    CLEARING_CORP = "CLEARING_CORP"


class SourceSystem(str, Enum):
    OMS = "OMS"
    BROKER_CONFIRM = "BROKER_CONFIRM"
    CUSTODIAN_STATEMENT = "CUSTODIAN_STATEMENT"


class Segment(str, Enum):
    NORMAL = "NORMAL"
    TFT = "TFT"


class NetDirection(str, Enum):
    PAY_IN = "PAY_IN"
    PAY_OUT = "PAY_OUT"


class ObligationStage(str, Enum):
    PROVISIONAL = "PROVISIONAL"
    FINAL = "FINAL"


class ObligationStatus(str, Enum):
    PENDING = "PENDING"
    SSI_VALIDATED = "SSI_VALIDATED"
    MATCHED = "MATCHED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    INSTRUCTED = "INSTRUCTED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    AUCTION = "AUCTION"
    CLOSED_OUT = "CLOSED_OUT"


class MatchStatus(str, Enum):
    UNMATCHED = "UNMATCHED"
    MATCHED = "MATCHED"
    BREAK = "BREAK"


class ConfirmationStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    LATE = "LATE"


class BreakType(str, Enum):
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    SSI_MISSING_OR_INCORRECT = "SSI_MISSING_OR_INCORRECT"
    LATE_CONFIRMATION = "LATE_CONFIRMATION"
    COUNTERPARTY_FAIL = "COUNTERPARTY_FAIL"
    CORPORATE_ACTION_CONFLICT = "CORPORATE_ACTION_CONFLICT"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class BreakStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    RESOLVED = "RESOLVED"


class Depository(str, Enum):
    NSDL = "NSDL"
    CDSL = "CDSL"


class InstructionStatus(str, Enum):
    GENERATED = "GENERATED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"


class AuctionOutcome(str, Enum):
    AUCTION_SUCCESS = "AUCTION_SUCCESS"
    CLOSED_OUT = "CLOSED_OUT"


class AuctionStatus(str, Enum):
    INITIATED = "INITIATED"
    AUCTION_HELD = "AUCTION_HELD"
    SETTLED = "SETTLED"
    CLOSED_OUT = "CLOSED_OUT"


class InstructionDirection(str, Enum):
    DELIVER = "DELIVER"
    RECEIVE = "RECEIVE"
