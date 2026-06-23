"""
ISO 20022 Settlement Message Formatter.

Generates structured XML messages in the ISO 20022 sese.023
(SecuritiesSettlementTransactionInstruction) format from internal
settlement instructions.

This replaces the legacy SWIFT MT540/MT542 (deliver/receive free) and
MT543/MT541 (deliver/receive against payment) message formats, in line
with the SWIFT MT message retirement timeline (Nov 2025).

Message structure follows ISO 20022 schema:
  sese.023.001.09 — Securities Settlement Transaction Instruction

Key mappings:
  - ISIN → FinancialInstrumentIdentification
  - DP ID → SafekeepingAccount
  - Settlement Bank → CashAccount
  - Quantity → QuantityOfFinancialInstrument
  - Settlement Value → SettlementAmount
  - Direction → SecuritiesMovementType (DELI/RECE)
  - Depository → PlaceOfSettlement

This is deterministic formatting — no LLM reasoning.
"""

import uuid
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

from src.models.database import SettlementInstruction
from src.models.enums import Depository, InstructionDirection
from src.utils.clock import utcnow


BIC_MAPPING = {
    "HDFC Bank": "HDFCINBB",
    "ICICI Bank": "ABORINBB",
    "Kotak Mahindra Bank": "ABORINBB",
    "Axis Bank": "UTIBINBB",
    "Deutsche Bank": "DEUTDEFF",
    "HSBC India": "HABORINBB",
    "Standard Chartered": "SCBLINBB",
    "Citibank India": "CITIINBX",
    "BNP Paribas": "BNPAINBB",
    "State Bank of India": "SBININBB",
    "JP Morgan Chase": "CHASINBX",
    "NSE Clearing Account": "NSCCLINB",
    "BSE Clearing Account": "ICCLLINB",
}

DEPOSITORY_BIC = {
    Depository.NSDL: "NSDLINBB",
    Depository.CDSL: "CDSLINBB",
}

NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:sese.023.001.09"


def format_iso20022(
    instruction: SettlementInstruction,
    sender_bic: str = "ABORINBB",
    message_id: str | None = None,
) -> str:
    """Format a settlement instruction as an ISO 20022 sese.023 XML message."""
    if message_id is None:
        message_id = f"SESE023-{uuid.uuid4().hex[:12].upper()}"

    root = Element("Document", xmlns=NAMESPACE)
    sctrs = SubElement(root, "SctiesSttlmTxInstr")

    # Message identification
    msg_id = SubElement(sctrs, "Id")
    SubElement(msg_id, "Id").text = message_id

    # Settlement type indicator
    sttlm_tp_and_addtl_params = SubElement(sctrs, "SttlmTpAndAddtlParams")
    sctrs_mvmnt_tp = SubElement(sttlm_tp_and_addtl_params, "SctiesMvmntTp")
    if instruction.direction == InstructionDirection.DELIVER:
        sctrs_mvmnt_tp.text = "DELI"
    else:
        sctrs_mvmnt_tp.text = "RECE"
    SubElement(sttlm_tp_and_addtl_params, "Pmt").text = "APMT"

    # Trade details
    trad_dtls = SubElement(sctrs, "TradDtls")
    trad_dt = SubElement(trad_dtls, "TradDt")
    SubElement(trad_dt, "Dt").text = utcnow().strftime("%Y-%m-%d")

    sttlm_dt = SubElement(trad_dtls, "SttlmDt")
    SubElement(sttlm_dt, "Dt").text = utcnow().strftime("%Y-%m-%d")

    # Financial instrument identification
    fin_instrm_id = SubElement(sctrs, "FinInstrmId")
    SubElement(fin_instrm_id, "ISIN").text = instruction.isin

    # Quantity of financial instrument
    qty_and_acct_dtls = SubElement(sctrs, "QtyAndAcctDtls")
    sttlm_qty = SubElement(qty_and_acct_dtls, "SttlmQty")
    SubElement(sttlm_qty, "Qty").text = str(instruction.quantity)

    # Safekeeping account (DP account)
    sfkpg_acct = SubElement(qty_and_acct_dtls, "SfkpgAcct")
    SubElement(sfkpg_acct, "Id").text = instruction.dp_account
    sfkpg_plc = SubElement(sfkpg_acct, "SfkpgPlc")
    dep_bic = DEPOSITORY_BIC.get(instruction.depository, "NSDLINBB")
    SubElement(sfkpg_plc, "Id").text = dep_bic

    # Settlement amount
    sttlm_amt = SubElement(sctrs, "SttlmAmt")
    amt = SubElement(sttlm_amt, "Amt", Ccy="INR")
    amt.text = f"{float(instruction.settlement_value):.2f}"

    # Delivering / receiving settlement parties
    dlvrgSttlmPties = SubElement(sctrs, "DlvrgSttlmPties")
    dpty1 = SubElement(dlvrgSttlmPties, "Dpstry")
    dpty1_id = SubElement(dpty1, "Id")
    SubElement(dpty1_id, "AnyBIC").text = dep_bic

    pty1 = SubElement(dlvrgSttlmPties, "Pty1")
    pty1_id = SubElement(pty1, "Id")
    SubElement(pty1_id, "AnyBIC").text = sender_bic

    # Cash settlement parties
    csh_pties = SubElement(sctrs, "CshPties")
    csh_acct_ownr = SubElement(csh_pties, "Acct")
    csh_acct_id = SubElement(csh_acct_ownr, "Id")
    SubElement(csh_acct_id, "IBAN").text = instruction.bank_account

    bank_bic = BIC_MAPPING.get(instruction.settlement_bank, "HDFCINBB")
    csh_agt = SubElement(csh_pties, "Agt")
    csh_agt_id = SubElement(csh_agt, "Id")
    SubElement(csh_agt_id, "AnyBIC").text = bank_bic

    # Supplementary data — internal reference
    splmtry_data = SubElement(sctrs, "SplmtryData")
    SubElement(splmtry_data, "InstrId").text = instruction.instruction_id
    SubElement(splmtry_data, "OblgtnId").text = instruction.obligation_id
    SubElement(splmtry_data, "DpId").text = instruction.dp_id

    xml_str = tostring(root, encoding="unicode")
    return parseString(xml_str).toprettyxml(indent="  ", encoding=None)


def format_batch(
    instructions: list[SettlementInstruction],
    sender_bic: str = "ABORINBB",
) -> list[dict]:
    """Format a batch of instructions, returning list of {instruction_id, xml}."""
    results = []
    for instr in instructions:
        xml = format_iso20022(instr, sender_bic)
        results.append({
            "instruction_id": instr.instruction_id,
            "obligation_id": instr.obligation_id,
            "isin": instr.isin,
            "direction": instr.direction.value,
            "message_type": "sese.023.001.09",
            "xml": xml,
        })
    return results


def get_message_summary(messages: list[dict]) -> dict:
    """Summarize a batch of ISO 20022 messages."""
    deliver_count = sum(1 for m in messages if m["direction"] == "DELIVER")
    receive_count = sum(1 for m in messages if m["direction"] == "RECEIVE")

    return {
        "total_messages": len(messages),
        "deliver_instructions": deliver_count,
        "receive_instructions": receive_count,
        "message_type": "sese.023.001.09",
        "format": "ISO 20022 XML",
    }
