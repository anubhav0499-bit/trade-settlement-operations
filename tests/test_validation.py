"""Stress tests for input validation in the trade normalizer."""

import pytest
from src.ingestion.normalizer import (
    ValidationError,
    _validate_isin,
    _validate_quantity,
    _validate_price,
    _validate_enum,
    normalize_oms_trades,
    normalize_broker_confirmations,
    normalize_custodian_statements,
)
from src.models.enums import Exchange, BuySell, SettlementCycle
from decimal import Decimal


class TestISINValidation:
    def test_valid_isin(self):
        assert _validate_isin("INE002A01018", "T1") == "INE002A01018"

    def test_valid_isin_with_whitespace(self):
        assert _validate_isin("  INE002A01018  ", "T1") == "INE002A01018"

    def test_empty_isin(self):
        with pytest.raises(ValidationError, match="invalid ISIN"):
            _validate_isin("", "T1")

    def test_too_short(self):
        with pytest.raises(ValidationError, match="invalid ISIN"):
            _validate_isin("INE002", "T1")

    def test_too_long(self):
        with pytest.raises(ValidationError, match="invalid ISIN"):
            _validate_isin("INE002A010189999", "T1")

    def test_lowercase_rejected(self):
        with pytest.raises(ValidationError):
            _validate_isin("ine002a01018", "T1")

    def test_invalid_check_digit(self):
        # Last char must be a digit
        with pytest.raises(ValidationError):
            _validate_isin("INE002A0101X", "T1")

    def test_special_characters(self):
        with pytest.raises(ValidationError):
            _validate_isin("INE002A01@18", "T1")

    def test_sql_injection_attempt(self):
        with pytest.raises(ValidationError):
            _validate_isin("'; DROP TABLE--", "T1")


class TestQuantityValidation:
    def test_valid_quantity(self):
        assert _validate_quantity("100", "T1") == 100

    def test_zero_rejected(self):
        with pytest.raises(ValidationError, match="out of bounds"):
            _validate_quantity("0", "T1")

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="out of bounds"):
            _validate_quantity("-50", "T1")

    def test_above_max_rejected(self):
        with pytest.raises(ValidationError, match="out of bounds"):
            _validate_quantity("10000001", "T1")

    def test_at_max(self):
        assert _validate_quantity("10000000", "T1") == 10_000_000

    def test_non_numeric(self):
        with pytest.raises(ValidationError, match="invalid quantity"):
            _validate_quantity("abc", "T1")

    def test_float_string(self):
        with pytest.raises(ValidationError, match="invalid quantity"):
            _validate_quantity("100.5", "T1")

    def test_none_rejected(self):
        with pytest.raises(ValidationError, match="invalid quantity"):
            _validate_quantity(None, "T1")


class TestPriceValidation:
    def test_valid_price(self):
        assert _validate_price("2900.50", "T1") == Decimal("2900.50")

    def test_zero_price_rejected(self):
        with pytest.raises(ValidationError, match="out of bounds"):
            _validate_price("0", "T1")

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError, match="out of bounds"):
            _validate_price("-100", "T1")

    def test_above_max_rejected(self):
        with pytest.raises(ValidationError, match="out of bounds"):
            _validate_price("1000000", "T1")

    def test_at_max(self):
        assert _validate_price("999999.9999", "T1") == Decimal("999999.9999")

    def test_non_numeric(self):
        with pytest.raises(ValidationError, match="invalid price"):
            _validate_price("abc", "T1")

    def test_very_small_price(self):
        assert _validate_price("0.01", "T1") == Decimal("0.01")


class TestEnumValidation:
    def test_valid_exchange(self):
        assert _validate_enum(Exchange, "NSE", "exchange", "T1") == Exchange.NSE

    def test_invalid_exchange(self):
        with pytest.raises(ValidationError, match="invalid exchange"):
            _validate_enum(Exchange, "NASDAQ", "exchange", "T1")

    def test_valid_buy_sell(self):
        assert _validate_enum(BuySell, "BUY", "buy_sell", "T1") == BuySell.BUY

    def test_empty_enum(self):
        with pytest.raises(ValidationError):
            _validate_enum(Exchange, "", "exchange", "T1")


class TestNormalizerSkipsInvalidRecords:
    def test_oms_skips_bad_isin(self):
        records = [
            {
                "trade_id": "T1",
                "isin": "INVALID",
                "security_name": "TEST",
                "quantity": "100",
                "price": "100.00",
                "trade_date": "2026-06-01",
                "settlement_date": "2026-06-02",
                "settlement_cycle": "T1",
                "counterparty_id": "BRK-001",
                "counterparty_type": "BROKER",
                "exchange": "NSE",
                "buy_sell": "BUY",
                "currency": "INR",
            }
        ]
        result = normalize_oms_trades(records)
        assert len(result) == 0

    def test_oms_skips_negative_quantity(self):
        records = [
            {
                "trade_id": "T2",
                "isin": "INE002A01018",
                "security_name": "TEST",
                "quantity": "-10",
                "price": "100.00",
                "trade_date": "2026-06-01",
                "settlement_date": "2026-06-02",
                "settlement_cycle": "T1",
                "counterparty_id": "BRK-001",
                "counterparty_type": "BROKER",
                "exchange": "NSE",
                "buy_sell": "BUY",
                "currency": "INR",
            }
        ]
        result = normalize_oms_trades(records)
        assert len(result) == 0

    def test_oms_accepts_valid_record(self):
        records = [
            {
                "trade_id": "T3",
                "isin": "INE002A01018",
                "security_name": "RELIANCE",
                "quantity": "100",
                "price": "2900.00",
                "trade_date": "2026-06-01",
                "settlement_date": "2026-06-02",
                "settlement_cycle": "T1",
                "counterparty_id": "BRK-001",
                "counterparty_type": "BROKER",
                "exchange": "NSE",
                "buy_sell": "BUY",
                "currency": "INR",
            }
        ]
        result = normalize_oms_trades(records)
        assert len(result) == 1
        assert result[0].isin == "INE002A01018"

    def test_mixed_valid_and_invalid(self):
        valid = {
            "trade_id": "T4",
            "isin": "INE002A01018",
            "security_name": "RELIANCE",
            "quantity": "100",
            "price": "2900.00",
            "trade_date": "2026-06-01",
            "settlement_date": "2026-06-02",
            "settlement_cycle": "T1",
            "counterparty_id": "BRK-001",
            "counterparty_type": "BROKER",
            "exchange": "NSE",
            "buy_sell": "BUY",
            "currency": "INR",
        }
        invalid = {**valid, "trade_id": "T5", "isin": "GARBAGE"}
        result = normalize_oms_trades([valid, invalid, valid.copy()])
        # Two valid, one invalid — but trade_id T4 appears twice with same PK
        assert len(result) == 2

    def test_broker_skips_invalid_side(self):
        records = [
            {
                "TradeRef": "B1",
                "ISIN_Code": "INE002A01018",
                "Scrip": "RELIANCE",
                "Qty": "100",
                "Rate": "2900.00",
                "TradeDay": "01-Jun-2026",
                "SettleDay": "02-Jun-2026",
                "Cycle": "T1",
                "BrokerCode": "BRK-001",
                "Exchange_Code": "NSE",
                "Side": "X",  # invalid
                "CCY": "INR",
            }
        ]
        result = normalize_broker_confirmations(records)
        assert len(result) == 0

    def test_custodian_missing_field(self):
        records = [
            {
                "original_trade_ref": "C1",
                "isin": "INE002A01018",
                # missing qty
                "exec_price": "2900.00",
                "trade_dt": "2026-06-01",
                "settle_dt": "2026-06-02",
                "cycle": "T1",
                "custodian_code": "CUS-001",
                "exch": "NSE",
                "direction": "BUY",
                "ccy": "INR",
                "security_desc": "RELIANCE",
            }
        ]
        result = normalize_custodian_statements(records)
        assert len(result) == 0
