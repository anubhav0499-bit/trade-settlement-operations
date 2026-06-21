"""
Matching Engine (§4).

Two-way match: internal net obligation vs counterparty net obligation (post-netting).
Match keys: ISIN, net quantity, VWAP price (±tolerance), settlement date, counterparty ID.

All matching logic is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.database import Obligation, BreakRecord
from src.models.enums import (
    BreakStatus,
    BreakType,
    MatchStatus,
    ObligationStatus,
    Severity,
)
from src.utils.config_loader import get_matching_config

import uuid


@dataclass
class MatchResult:
    internal_obligation_id: str
    counterparty_obligation_id: str | None
    status: MatchStatus
    break_type: BreakType | None
    details: dict


def _price_within_tolerance(
    price_a: Decimal,
    price_b: Decimal,
    tolerance_pct: float,
) -> bool:
    if price_a == 0 and price_b == 0:
        return True
    if price_a == 0 or price_b == 0:
        return False
    pct_diff = abs(price_a - price_b) / price_a * 100
    return float(pct_diff) <= tolerance_pct


def match_obligations(
    internal: list[Obligation],
    counterparty: list[Obligation],
    config: dict | None = None,
) -> list[MatchResult]:
    """Match internal obligations against counterparty obligations.

    Args:
        internal: Obligations from OMS (internal view)
        counterparty: Obligations from broker or custodian (external view)
        config: Matching tolerances (loaded from YAML if not provided)

    Returns:
        List of MatchResult for each internal obligation.
    """
    if config is None:
        config = get_matching_config()

    price_tolerance = config.get("price_tolerance_pct", 0.5)
    qty_tolerance = config.get("quantity_tolerance_abs", 0)

    # Index counterparty obligations by match key for O(1) lookup
    cp_index: dict[tuple, list[Obligation]] = {}
    for ob in counterparty:
        key = (ob.isin, ob.settlement_date, ob.counterparty_id)
        cp_index.setdefault(key, []).append(ob)

    # Track which counterparty obligations have been consumed
    consumed_cp: set[str] = set()
    results = []

    for internal_ob in internal:
        if internal_ob.status not in (
            ObligationStatus.SSI_VALIDATED,
            ObligationStatus.PENDING,
        ):
            continue

        lookup_key = (internal_ob.isin, internal_ob.settlement_date, internal_ob.counterparty_id)
        candidates = cp_index.get(lookup_key, [])

        # Filter out already-consumed candidates
        available = [c for c in candidates if c.obligation_id not in consumed_cp]

        if not available:
            results.append(MatchResult(
                internal_obligation_id=internal_ob.obligation_id,
                counterparty_obligation_id=None,
                status=MatchStatus.UNMATCHED,
                break_type=None,
                details={"reason": "No counterparty obligation found for match key"},
            ))
            internal_ob.match_status = MatchStatus.UNMATCHED
            continue

        # Try to find an exact or tolerance match
        matched = False
        for cp_ob in available:
            qty_match = abs(internal_ob.net_quantity - cp_ob.net_quantity) <= qty_tolerance
            price_match = _price_within_tolerance(
                Decimal(str(internal_ob.vwap_price)),
                Decimal(str(cp_ob.vwap_price)),
                price_tolerance,
            )
            direction_match = internal_ob.net_direction == cp_ob.net_direction

            if qty_match and price_match and direction_match:
                consumed_cp.add(cp_ob.obligation_id)
                results.append(MatchResult(
                    internal_obligation_id=internal_ob.obligation_id,
                    counterparty_obligation_id=cp_ob.obligation_id,
                    status=MatchStatus.MATCHED,
                    break_type=None,
                    details={},
                ))
                internal_ob.match_status = MatchStatus.MATCHED
                internal_ob.status = ObligationStatus.MATCHED
                cp_ob.match_status = MatchStatus.MATCHED
                matched = True
                break

        if not matched:
            # Determine break type from the best available candidate
            best_cp = available[0]
            consumed_cp.add(best_cp.obligation_id)
            break_type, details = _classify_mismatch(internal_ob, best_cp, price_tolerance)

            results.append(MatchResult(
                internal_obligation_id=internal_ob.obligation_id,
                counterparty_obligation_id=best_cp.obligation_id,
                status=MatchStatus.BREAK,
                break_type=break_type,
                details=details,
            ))
            internal_ob.match_status = MatchStatus.BREAK
            best_cp.match_status = MatchStatus.BREAK

    return results


def _classify_mismatch(
    internal: Obligation,
    counterparty: Obligation,
    price_tolerance: float,
) -> tuple[BreakType, dict]:
    """Classify the type of mismatch between two obligations."""
    qty_diff = abs(internal.net_quantity - counterparty.net_quantity)
    price_diff_pct = 0.0
    if internal.vwap_price and float(internal.vwap_price) > 0:
        price_diff_pct = (
            abs(float(internal.vwap_price) - float(counterparty.vwap_price))
            / float(internal.vwap_price) * 100
        )

    details = {
        "internal_qty": internal.net_quantity,
        "counterparty_qty": counterparty.net_quantity,
        "qty_diff": qty_diff,
        "internal_price": str(internal.vwap_price),
        "counterparty_price": str(counterparty.vwap_price),
        "price_diff_pct": round(price_diff_pct, 4),
        "internal_direction": internal.net_direction.value,
        "counterparty_direction": counterparty.net_direction.value,
    }

    if qty_diff > 0 and price_diff_pct > price_tolerance:
        # Both differ — report quantity first (usually the bigger issue)
        return BreakType.QUANTITY_MISMATCH, details
    elif qty_diff > 0:
        return BreakType.QUANTITY_MISMATCH, details
    elif price_diff_pct > price_tolerance:
        return BreakType.PRICE_MISMATCH, details
    elif internal.net_direction != counterparty.net_direction:
        return BreakType.QUANTITY_MISMATCH, details  # direction mismatch implies qty sign flip

    return BreakType.QUANTITY_MISMATCH, details


def create_break_records(
    session: Session,
    match_results: list[MatchResult],
    obligations_by_id: dict[str, Obligation],
) -> list[BreakRecord]:
    """Create BreakRecord entries for all BREAK match results."""
    breaks = []
    for result in match_results:
        if result.status != MatchStatus.BREAK:
            continue

        ob = obligations_by_id.get(result.internal_obligation_id)
        if ob is None:
            continue

        severity = _compute_severity(ob)
        break_record = BreakRecord(
            break_id=str(uuid.uuid4()),
            obligation_id=ob.obligation_id,
            break_type=result.break_type,
            severity=severity,
            value_at_risk=ob.net_value,
            age_hours=0,
            age_days=0,
            status=BreakStatus.OPEN,
            escalation_level=0,
        )
        breaks.append(break_record)
        session.add(break_record)

    session.commit()
    return breaks


def _compute_severity(obligation: Obligation) -> Severity:
    """Compute severity based on value at risk."""
    val = float(obligation.net_value)
    if val < 500_000:
        return Severity.LOW
    elif val < 2_500_000:
        return Severity.MEDIUM
    return Severity.HIGH
