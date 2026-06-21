"""Reference data for synthetic generation: ISINs, counterparties, SSI records."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class SecurityInfo:
    isin: str
    name: str
    exchange: str  # NSE / BSE / BOTH
    price_low: float
    price_high: float
    t0_eligible: bool  # top-500 eligible for T+0


SECURITIES: list[SecurityInfo] = [
    # Large-cap Nifty 50 (T+0 eligible)
    SecurityInfo("INE002A01018", "RELIANCE INDUSTRIES", "BOTH", 2750, 3050, True),
    SecurityInfo("INE467B01029", "TATA CONSULTANCY SERVICES", "BOTH", 3400, 3850, True),
    SecurityInfo("INE009A01021", "INFOSYS", "BOTH", 1450, 1650, True),
    SecurityInfo("INE040A01034", "HDFC BANK", "BOTH", 1600, 1820, True),
    SecurityInfo("INE154A01025", "ITC", "BOTH", 430, 480, True),
    SecurityInfo("INE090A01021", "ICICI BANK", "BOTH", 1150, 1320, True),
    SecurityInfo("INE585B01010", "MARUTI SUZUKI", "BOTH", 11800, 13200, True),
    SecurityInfo("INE062A01020", "STATE BANK OF INDIA", "BOTH", 780, 870, True),
    SecurityInfo("INE030A01027", "HINDUSTAN UNILEVER", "BOTH", 2350, 2650, True),
    SecurityInfo("INE176A01028", "BAJAJ FINANCE", "BOTH", 6800, 7500, True),
    SecurityInfo("INE669E01016", "BHARTI AIRTEL", "BOTH", 1550, 1720, True),
    SecurityInfo("INE021A01026", "ASIAN PAINTS", "BOTH", 2800, 3100, True),
    SecurityInfo("INE018A01030", "LARSEN & TOUBRO", "BOTH", 3300, 3650, True),
    SecurityInfo("INE019A01038", "WIPRO", "BOTH", 440, 510, True),
    SecurityInfo("INE628A01036", "ULTRATECH CEMENT", "BOTH", 9500, 10800, True),
    SecurityInfo("INE047A01021", "TITAN COMPANY", "BOTH", 3100, 3500, True),
    SecurityInfo("INE160A01022", "NESTLE INDIA", "BOTH", 2200, 2500, True),
    SecurityInfo("INE238A01034", "AXIS BANK", "BOTH", 1050, 1200, True),
    SecurityInfo("INE101A01026", "KOTAK MAHINDRA BANK", "NSE", 1750, 1950, True),
    SecurityInfo("INE795G01014", "POWER GRID CORPORATION", "BOTH", 290, 340, True),
    # Mid-cap (T+0 eligible, top-500)
    SecurityInfo("INE129A01019", "GAIL (INDIA)", "BOTH", 175, 210, True),
    SecurityInfo("INE752E01010", "PIDILITE INDUSTRIES", "BOTH", 2650, 3000, True),
    SecurityInfo("INE726G01019", "TRENT", "BOTH", 5200, 6000, True),
    SecurityInfo("INE216A01030", "BHARAT ELECTRONICS", "BOTH", 280, 340, True),
    SecurityInfo("INE259A01022", "GODREJ CONSUMER PRODUCTS", "NSE", 1200, 1400, True),
    SecurityInfo("INE140A01024", "BANK OF BARODA", "BOTH", 230, 275, True),
    SecurityInfo("INE092T01019", "ZOMATO", "NSE", 210, 260, True),
    SecurityInfo("INE205A01025", "SIEMENS", "BOTH", 5800, 6600, True),
    SecurityInfo("INE397D01024", "BHARAT FORGE", "BOTH", 1150, 1350, True),
    SecurityInfo("INE148I01020", "POLICYBAZAAR (PB FINTECH)", "NSE", 1600, 1900, True),
    # Small/Mid-cap (NOT T+0 eligible)
    SecurityInfo("INE274J01014", "HAPPIEST MINDS TECHNOLOGIES", "NSE", 680, 820, False),
    SecurityInfo("INE545U01014", "NAZARA TECHNOLOGIES", "NSE", 750, 920, False),
    SecurityInfo("INE124N01016", "ANGEL ONE", "NSE", 2100, 2600, False),
    SecurityInfo("INE483S01020", "LATENT VIEW ANALYTICS", "NSE", 420, 540, False),
    SecurityInfo("INE00IN01015", "PAYTM (ONE97)", "BOTH", 380, 480, False),
    SecurityInfo("INE03YQ01011", "CLEAN SCIENCE & TECHNOLOGY", "NSE", 1250, 1500, False),
    SecurityInfo("INE00WK01013", "ROUTE MOBILE", "NSE", 1300, 1550, False),
    SecurityInfo("INE03VK01010", "CARTRADE TECH", "NSE", 550, 680, False),
    SecurityInfo("INE148O01018", "EID PARRY (INDIA)", "NSE", 650, 780, False),
    SecurityInfo("INE761H01022", "AFFLE (INDIA)", "NSE", 1050, 1250, False),
    # Illiquid / low-priced
    SecurityInfo("INE059B01024", "RAIN INDUSTRIES", "BOTH", 120, 165, False),
    SecurityInfo("INE883A01011", "FINOLEX CABLES", "BOTH", 850, 1000, False),
    SecurityInfo("INE576I01022", "GRINDWELL NORTON", "NSE", 1900, 2200, False),
    SecurityInfo("INE550C01020", "TV TODAY NETWORK", "NSE", 180, 240, False),
    SecurityInfo("INE104S01021", "UJJIVAN SFB", "NSE", 38, 52, False),
    SecurityInfo("INE121A01024", "CHOLAMANDALAM INVESTMENT", "BOTH", 1350, 1550, True),
    SecurityInfo("INE917I01010", "AVENUE SUPERMARTS (DMART)", "NSE", 3600, 4100, True),
    SecurityInfo("INE528G01035", "SHRIRAM FINANCE", "BOTH", 2400, 2750, True),
    SecurityInfo("INE044A01036", "DABUR INDIA", "BOTH", 540, 620, True),
    SecurityInfo("INE361B01024", "CANARA BANK", "BOTH", 95, 118, True),
]


@dataclass
class CounterpartyInfo:
    counterparty_id: str
    name: str
    counterparty_type: str  # BROKER / CUSTODIAN / CLEARING_CORP
    exchange_membership: str  # NSE / BSE / BOTH


COUNTERPARTIES: list[CounterpartyInfo] = [
    # Brokers (6)
    CounterpartyInfo("BRK-001", "Zerodha Securities", "BROKER", "BOTH"),
    CounterpartyInfo("BRK-002", "ICICI Securities", "BROKER", "BOTH"),
    CounterpartyInfo("BRK-003", "HDFC Securities", "BROKER", "BOTH"),
    CounterpartyInfo("BRK-004", "Kotak Securities", "BROKER", "NSE"),
    CounterpartyInfo("BRK-005", "Motilal Oswal Securities", "BROKER", "BOTH"),
    CounterpartyInfo("BRK-006", "Axis Securities", "BROKER", "BSE"),
    # Custodians (5)
    CounterpartyInfo("CUS-001", "Deutsche Bank Custodial Services", "CUSTODIAN", "BOTH"),
    CounterpartyInfo("CUS-002", "HSBC Custody & Clearing", "CUSTODIAN", "BOTH"),
    CounterpartyInfo("CUS-003", "Standard Chartered Custody", "CUSTODIAN", "BOTH"),
    CounterpartyInfo("CUS-004", "Citibank Custodial", "CUSTODIAN", "NSE"),
    CounterpartyInfo("CUS-005", "BNP Paribas Custody", "CUSTODIAN", "BOTH"),
    # Clearing Corporations (2)
    CounterpartyInfo("NSCCL", "NSE Clearing Corporation Ltd", "CLEARING_CORP", "NSE"),
    CounterpartyInfo("ICCL", "Indian Clearing Corporation Ltd", "CLEARING_CORP", "BSE"),
    # Broker-Custodians (2) — act as both
    CounterpartyInfo("BC-001", "SBI Cap Securities (Custodian)", "CUSTODIAN", "BOTH"),
    CounterpartyInfo("BC-002", "JP Morgan India Custody", "CUSTODIAN", "BOTH"),
]


@dataclass
class SSIInfo:
    counterparty_id: str
    settlement_bank: str
    bank_account: str
    dp_id: str
    dp_account: str
    depository: str  # NSDL / CDSL
    effective_from: str  # YYYY-MM-DD
    effective_to: str | None  # None = current


SSI_RECORDS: list[SSIInfo] = [
    # Active SSIs for all counterparties
    SSIInfo("BRK-001", "HDFC Bank", "00112233445566", "IN300513", "12345678", "NSDL", "2025-01-01", None),
    SSIInfo("BRK-002", "ICICI Bank", "00223344556677", "IN301549", "23456789", "NSDL", "2025-01-01", None),
    SSIInfo("BRK-003", "HDFC Bank", "00334455667788", "IN300484", "34567890", "CDSL", "2025-01-01", None),
    SSIInfo("BRK-004", "Kotak Mahindra Bank", "00445566778899", "IN300757", "45678901", "NSDL", "2025-01-01", None),
    SSIInfo("BRK-005", "Axis Bank", "00556677889900", "IN301774", "56789012", "NSDL", "2025-01-01", None),
    SSIInfo("BRK-006", "Axis Bank", "00667788990011", "IN302679", "67890123", "CDSL", "2025-01-01", None),
    SSIInfo("CUS-001", "Deutsche Bank", "DE1122334455", "IN300394", "78901234", "NSDL", "2025-01-01", None),
    SSIInfo("CUS-002", "HSBC India", "HB2233445566", "IN301330", "89012345", "NSDL", "2025-01-01", None),
    SSIInfo("CUS-003", "Standard Chartered", "SC3344556677", "IN300239", "90123456", "CDSL", "2025-01-01", None),
    SSIInfo("CUS-004", "Citibank India", "CT4455667788", "IN300079", "01234567", "NSDL", "2025-01-01", None),
    SSIInfo("CUS-005", "BNP Paribas", "BN5566778899", "IN302269", "12340987", "NSDL", "2025-01-01", None),
    SSIInfo("NSCCL", "NSE Clearing Account", "NSCCL00001", "IN300476", "NSCCL001", "NSDL", "2024-01-01", None),
    SSIInfo("ICCL", "BSE Clearing Account", "ICCL000001", "IN300177", "ICCL0001", "CDSL", "2024-01-01", None),
    SSIInfo("BC-001", "State Bank of India", "SB6677889900", "IN300183", "43210987", "NSDL", "2025-01-01", None),
    SSIInfo("BC-002", "JP Morgan Chase", "JP7788990011", "IN301098", "54321098", "NSDL", "2025-01-01", None),
    # Historical SSI versions (superseded — effective_to is set)
    SSIInfo("CUS-001", "Deutsche Bank", "DE0000OLD001", "IN300394", "78901234", "NSDL", "2024-01-01", "2024-12-31"),
    SSIInfo("BRK-003", "ICICI Bank", "OLD_HDFC_003", "IN300484", "34567890", "CDSL", "2024-06-01", "2024-12-31"),
    SSIInfo("CUS-005", "BNP Paribas", "BN_OLD_ACCT", "IN302269", "12340987", "NSDL", "2024-01-01", "2024-12-31"),
]


# June 2026 trading days (skip weekends, include 1 holiday)
TRADING_DAYS_JUNE_2026 = [
    "2026-06-01",  # Monday
    "2026-06-02",
    "2026-06-03",
    "2026-06-04",
    "2026-06-05",
    # 2026-06-06, 2026-06-07 = weekend
    "2026-06-08",
    "2026-06-09",
    "2026-06-10",
    "2026-06-11",
    "2026-06-12",
    # 2026-06-13, 2026-06-14 = weekend
    # 2026-06-15 = Eid ul-Adha (market holiday)
    "2026-06-16",
    "2026-06-17",
    "2026-06-18",
    "2026-06-19",
    # 2026-06-20, 2026-06-21 = weekend
    "2026-06-22",
    "2026-06-23",
    "2026-06-24",
    "2026-06-25",
    # 2026-06-26 = Muharram (market holiday)
    # 2026-06-27, 2026-06-28 = weekend
    "2026-06-29",
    "2026-06-30",
]
assert len(TRADING_DAYS_JUNE_2026) == 20
