"""
Options Exercise & Assignment Engine (European style, automatic at expiry).

ITM long option positions are exercised automatically unless the holder
submits a Don't-Exercise (DNE) instruction. Exercised quantity is assigned to
short positions in the same contract via a deterministically seeded random
draw — this is a settlement mechanism, not a prediction, so it must stay
reproducible and rule-based, never LLM-driven.
"""

import random
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.database import DerivativeContract, DerivativePosition
from src.models.enums import BuySell, OptionType


@dataclass
class ExerciseResult:
    contract_id: str
    counterparty_id: str
    position_id: str
    exercised_quantity: int
    is_itm: bool


@dataclass
class AssignmentResult:
    contract_id: str
    counterparty_id: str
    position_id: str
    assigned_quantity: int


def is_in_the_money(contract: DerivativeContract, fsp: Decimal) -> bool:
    if contract.option_type == OptionType.CALL:
        return fsp > Decimal(str(contract.strike_price))
    if contract.option_type == OptionType.PUT:
        return fsp < Decimal(str(contract.strike_price))
    return False


def exercise_long_positions(
    session: Session,
    contract: DerivativeContract,
    fsp: Decimal,
    dne_position_ids: set[str] | None = None,
) -> list[ExerciseResult]:
    """Auto-exercise ITM long option positions, honoring DNE opt-outs."""
    dne_position_ids = dne_position_ids or set()
    itm = is_in_the_money(contract, fsp)

    longs = (
        session.query(DerivativePosition)
        .filter(
            DerivativePosition.contract_id == contract.contract_id,
            DerivativePosition.buy_sell == BuySell.BUY,
        )
        .all()
    )

    results = []
    for pos in longs:
        exercised_qty = (
            pos.quantity if (itm and pos.position_id not in dne_position_ids) else 0
        )
        results.append(ExerciseResult(
            contract_id=contract.contract_id,
            counterparty_id=pos.counterparty_id,
            position_id=pos.position_id,
            exercised_quantity=exercised_qty,
            is_itm=itm,
        ))
    return results


def assign_short_positions(
    session: Session,
    contract: DerivativeContract,
    exercise_results: list[ExerciseResult],
    seed: int = 0,
) -> list[AssignmentResult]:
    """Randomly assign total exercised quantity across short positions, pro-rata by lots held."""
    total_exercised = sum(r.exercised_quantity for r in exercise_results)
    if total_exercised == 0:
        return []

    shorts = (
        session.query(DerivativePosition)
        .filter(
            DerivativePosition.contract_id == contract.contract_id,
            DerivativePosition.buy_sell == BuySell.SELL,
        )
        .all()
    )
    if not shorts:
        return []

    rng = random.Random(seed)
    # One pool entry per lot held short, shuffled, then assigned sequentially.
    pool = []
    for pos in shorts:
        pool.extend([pos.position_id] * pos.quantity)
    rng.shuffle(pool)

    to_assign = min(total_exercised, len(pool))
    assigned_units = pool[:to_assign]

    counts: dict[str, int] = {}
    for position_id in assigned_units:
        counts[position_id] = counts.get(position_id, 0) + 1

    by_id = {p.position_id: p for p in shorts}
    return [
        AssignmentResult(
            contract_id=contract.contract_id,
            counterparty_id=by_id[position_id].counterparty_id,
            position_id=position_id,
            assigned_quantity=qty,
        )
        for position_id, qty in counts.items()
    ]
