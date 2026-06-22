"""Stress tests for ISO 20022 settlement message formatter."""

import pytest
from xml.etree.ElementTree import fromstring
from decimal import Decimal

from src.models.database import SettlementInstruction
from src.models.enums import Depository, InstructionDirection, InstructionStatus
from src.instruction.iso20022_formatter import (
    BIC_MAPPING,
    DEPOSITORY_BIC,
    NAMESPACE,
    format_batch,
    format_iso20022,
    get_message_summary,
)


def _make_instruction(
    instruction_id="INSTR-001",
    obligation_id="OB-001",
    isin="INE002A01018",
    quantity=100,
    settlement_value=290_000,
    direction=InstructionDirection.DELIVER,
    dp_id="IN300001",
    dp_account="1234567890123456",
    settlement_bank="HDFC Bank",
    bank_account="HDFC0001234567890",
    depository=Depository.NSDL,
) -> SettlementInstruction:
    return SettlementInstruction(
        instruction_id=instruction_id,
        obligation_id=obligation_id,
        isin=isin,
        quantity=quantity,
        settlement_value=Decimal(str(settlement_value)),
        direction=direction,
        dp_id=dp_id,
        dp_account=dp_account,
        settlement_bank=settlement_bank,
        bank_account=bank_account,
        depository=depository,
    )


class TestFormatISO20022:
    def test_produces_valid_xml(self):
        instr = _make_instruction()
        xml_str = format_iso20022(instr)
        root = fromstring(xml_str)
        assert root.tag == f"{{{NAMESPACE}}}Document" or root.tag == "Document"

    def test_contains_isin(self):
        instr = _make_instruction(isin="INE009A01021")
        xml_str = format_iso20022(instr)
        assert "INE009A01021" in xml_str

    def test_deliver_direction(self):
        instr = _make_instruction(direction=InstructionDirection.DELIVER)
        xml_str = format_iso20022(instr)
        assert "<SctiesMvmntTp>DELI</SctiesMvmntTp>" in xml_str

    def test_receive_direction(self):
        instr = _make_instruction(direction=InstructionDirection.RECEIVE)
        xml_str = format_iso20022(instr)
        assert "<SctiesMvmntTp>RECE</SctiesMvmntTp>" in xml_str

    def test_payment_type_apmt(self):
        instr = _make_instruction()
        xml_str = format_iso20022(instr)
        assert "<Pmt>APMT</Pmt>" in xml_str

    def test_settlement_amount_formatted(self):
        instr = _make_instruction(settlement_value=1_234_567.89)
        xml_str = format_iso20022(instr)
        assert "1234567.89" in xml_str

    def test_currency_inr(self):
        instr = _make_instruction()
        xml_str = format_iso20022(instr)
        assert 'Ccy="INR"' in xml_str

    def test_nsdl_depository_bic(self):
        instr = _make_instruction(depository=Depository.NSDL)
        xml_str = format_iso20022(instr)
        assert "NSDLINBB" in xml_str

    def test_cdsl_depository_bic(self):
        instr = _make_instruction(depository=Depository.CDSL)
        xml_str = format_iso20022(instr)
        assert "CDSLINBB" in xml_str

    def test_bank_bic_mapping(self):
        instr = _make_instruction(settlement_bank="ICICI Bank")
        xml_str = format_iso20022(instr)
        assert BIC_MAPPING["ICICI Bank"] in xml_str

    def test_unknown_bank_defaults_to_hdfc(self):
        instr = _make_instruction(settlement_bank="Unknown Bank XYZ")
        xml_str = format_iso20022(instr)
        assert "HDFCINBB" in xml_str

    def test_custom_message_id(self):
        instr = _make_instruction()
        xml_str = format_iso20022(instr, message_id="CUSTOM-MSG-001")
        assert "CUSTOM-MSG-001" in xml_str

    def test_auto_generated_message_id(self):
        instr = _make_instruction()
        xml_str = format_iso20022(instr)
        assert "SESE023-" in xml_str

    def test_supplementary_data_references(self):
        instr = _make_instruction(
            instruction_id="INSTR-TEST",
            obligation_id="OB-TEST",
            dp_id="DP-TEST",
        )
        xml_str = format_iso20022(instr)
        assert "INSTR-TEST" in xml_str
        assert "OB-TEST" in xml_str
        assert "DP-TEST" in xml_str

    def test_dp_account_in_safekeeping(self):
        instr = _make_instruction(dp_account="9876543210123456")
        xml_str = format_iso20022(instr)
        assert "9876543210123456" in xml_str

    def test_bank_account_as_iban(self):
        instr = _make_instruction(bank_account="HDFC00099887766")
        xml_str = format_iso20022(instr)
        assert "HDFC00099887766" in xml_str

    def test_quantity_in_message(self):
        instr = _make_instruction(quantity=5000)
        xml_str = format_iso20022(instr)
        assert "<Qty>5000</Qty>" in xml_str

    def test_xml_is_indented(self):
        instr = _make_instruction()
        xml_str = format_iso20022(instr)
        assert "  <" in xml_str  # pretty-printed with indentation


class TestFormatBatch:
    def test_batch_returns_all(self):
        instructions = [
            _make_instruction(instruction_id=f"INSTR-{i}", obligation_id=f"OB-{i}")
            for i in range(3)
        ]
        results = format_batch(instructions)
        assert len(results) == 3

    def test_batch_structure(self):
        instr = _make_instruction(direction=InstructionDirection.DELIVER)
        results = format_batch([instr])
        r = results[0]
        assert r["instruction_id"] == "INSTR-001"
        assert r["obligation_id"] == "OB-001"
        assert r["isin"] == "INE002A01018"
        assert r["direction"] == "DELIVER"
        assert r["message_type"] == "sese.023.001.09"
        assert "<Document" in r["xml"]

    def test_batch_empty(self):
        assert format_batch([]) == []


class TestMessageSummary:
    def test_mixed_directions(self):
        messages = [
            {"direction": "DELIVER"},
            {"direction": "DELIVER"},
            {"direction": "RECEIVE"},
        ]
        summary = get_message_summary(messages)
        assert summary["total_messages"] == 3
        assert summary["deliver_instructions"] == 2
        assert summary["receive_instructions"] == 1
        assert summary["message_type"] == "sese.023.001.09"

    def test_empty_batch(self):
        summary = get_message_summary([])
        assert summary["total_messages"] == 0
