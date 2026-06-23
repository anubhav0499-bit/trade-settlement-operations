"""Tests for debt trade ingestion (CBRICS, RFQ, CCIL)."""

from decimal import Decimal

from src.models.enums import DebtTradeStatus, ProductSegment
from src.debt.trade_ingestion import (
    normalize_cbrics_trades,
    normalize_ccil_reports,
    normalize_rfq_trades,
)


class TestNormalizeCbricsTrades:
    def test_valid_record_normalized(self):
        records = [{
            "trade_id": "CB1",
            "isin": "INE001A01036",
            "quantity": "10",
            "price": "98.50",
            "buyer_id": "BRK-001",
            "seller_id": "BRK-002",
            "trade_date": "2026-06-23",
            "settlement_date": "2026-06-25",
        }]
        trades = normalize_cbrics_trades(records)
        assert len(trades) == 1
        assert trades[0].clean_price == Decimal("98.50")
        assert trades[0].product_segment == ProductSegment.DEBT_CORP_BOND
        assert trades[0].source == "CBRICS"
        assert trades[0].status == DebtTradeStatus.PENDING

    def test_invalid_isin_skipped(self):
        records = [{
            "trade_id": "CB1",
            "isin": "BAD-ISIN",
            "quantity": "10",
            "price": "98.50",
            "buyer_id": "BRK-001",
            "seller_id": "BRK-002",
            "trade_date": "2026-06-23",
            "settlement_date": "2026-06-25",
        }]
        assert normalize_cbrics_trades(records) == []

    def test_missing_field_skipped(self):
        records = [{"trade_id": "CB1", "isin": "INE001A01036"}]
        assert normalize_cbrics_trades(records) == []

    def test_empty_records(self):
        assert normalize_cbrics_trades([]) == []


class TestNormalizeRfqTrades:
    def test_valid_record_normalized(self):
        records = [{
            "DealRef": "RFQ1",
            "ISIN": "INE001A01036",
            "Quantity": "5",
            "Price": "99.00",
            "Buyer": "BRK-001",
            "Seller": "BRK-002",
            "TradeDate": "23-06-2026",
            "SettleDate": "25-06-2026",
        }]
        trades = normalize_rfq_trades(records)
        assert len(trades) == 1
        assert trades[0].source == "RFQ"

    def test_invalid_quantity_skipped(self):
        records = [{
            "DealRef": "RFQ1",
            "ISIN": "INE001A01036",
            "Quantity": "-5",
            "Price": "99.00",
            "Buyer": "BRK-001",
            "Seller": "BRK-002",
            "TradeDate": "23-06-2026",
            "SettleDate": "25-06-2026",
        }]
        assert normalize_rfq_trades(records) == []


class TestNormalizeCcilReports:
    def test_valid_record_normalized_as_gsec(self):
        records = [{
            "TradeNo": "CCIL1",
            "SecurityCode": "IN0020230012",
            "FaceValueTraded": "1000",
            "Price": "105.25",
            "Counterparty1": "BRK-001",
            "Counterparty2": "BRK-002",
            "TradeDate": "2026-06-23",
            "SettleDate": "2026-06-24",
        }]
        trades = normalize_ccil_reports(records)
        assert len(trades) == 1
        assert trades[0].product_segment == ProductSegment.DEBT_GSEC
        assert trades[0].source == "CCIL"

    def test_invalid_price_skipped(self):
        records = [{
            "TradeNo": "CCIL1",
            "SecurityCode": "IN0020230012",
            "FaceValueTraded": "1000",
            "Price": "not-a-price",
            "Counterparty1": "BRK-001",
            "Counterparty2": "BRK-002",
            "TradeDate": "2026-06-23",
            "SettleDate": "2026-06-24",
        }]
        assert normalize_ccil_reports(records) == []
