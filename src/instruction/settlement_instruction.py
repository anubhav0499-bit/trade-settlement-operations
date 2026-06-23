"""
Settlement Instruction Generation (§6).

For each CONFIRMED obligation, generate a settlement instruction using the
validated SSI fields (DP ID, settlement bank, account) from the golden-copy module.
Marks the obligation as INSTRUCTED once the instruction is generated.
"""

import uuid

from sqlalchemy.orm import Session

from src.models.database import Obligation, SettlementInstruction
from src.models.enums import (
    InstructionDirection,
    InstructionStatus,
    NetDirection,
    ObligationStatus,
)
from src.ssi.golden_copy import get_active_ssi
from src.utils.clock import utcnow


def generate_instruction(
    session: Session,
    obligation: Obligation,
) -> SettlementInstruction | None:
    """Generate a settlement instruction for a confirmed obligation.

    Returns None if SSI lookup fails (should not happen post-validation,
    but defensive).
    """
    if obligation.status != ObligationStatus.CONFIRMED:
        return None

    ssi = get_active_ssi(session, obligation.counterparty_id, obligation.settlement_date)
    if ssi is None:
        return None

    direction = (
        InstructionDirection.DELIVER
        if obligation.net_direction == NetDirection.PAY_IN
        else InstructionDirection.RECEIVE
    )

    instruction = SettlementInstruction(
        instruction_id=str(uuid.uuid4()),
        obligation_id=obligation.obligation_id,
        isin=obligation.isin,
        quantity=obligation.net_quantity,
        settlement_value=obligation.net_value,
        direction=direction,
        dp_id=ssi.dp_id,
        dp_account=ssi.dp_account,
        settlement_bank=ssi.settlement_bank,
        bank_account=ssi.bank_account,
        depository=ssi.depository,
        status=InstructionStatus.GENERATED,
        generated_at=utcnow(),
    )

    obligation.status = ObligationStatus.INSTRUCTED
    obligation.instruction_id = instruction.instruction_id

    session.add(instruction)
    session.commit()
    return instruction


def generate_all_instructions(
    session: Session,
    obligations: list[Obligation],
) -> list[SettlementInstruction]:
    """Generate settlement instructions for all confirmed obligations."""
    instructions = []
    for ob in obligations:
        if ob.status != ObligationStatus.CONFIRMED:
            continue
        instr = generate_instruction(session, ob)
        if instr:
            instructions.append(instr)
    return instructions
