"""
Multi-CM Hierarchy — TM-CM clearing for sub-TMs, and obligation aggregation
up that hierarchy.

A TM-CM or PCM can clear trades on behalf of sub-TMs and custodial
participants. Each clearing member's obligations are tracked individually in
the `Obligation` table by `counterparty_id`; this module aggregates that data
across a clearing member and all of its descendants in the hierarchy.

All computation is deterministic and rule-based — no LLM reasoning.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.database import ClearingMember, Obligation
from src.models.enums import CMType


def register_clearing_member(
    session: Session,
    cm_id: str,
    name: str,
    cm_type: CMType,
    net_worth: Decimal,
    security_deposit: Decimal,
    parent_cm_id: str | None = None,
) -> ClearingMember:
    member = ClearingMember(
        cm_id=cm_id,
        name=name,
        cm_type=cm_type,
        parent_cm_id=parent_cm_id,
        net_worth=net_worth,
        security_deposit=security_deposit,
    )
    session.add(member)
    session.commit()
    return member


def get_sub_tms(session: Session, cm_id: str) -> list[ClearingMember]:
    """Direct children of this clearing member in the hierarchy."""
    return session.query(ClearingMember).filter_by(parent_cm_id=cm_id).all()


def get_all_descendant_ids(session: Session, cm_id: str) -> list[str]:
    """The clearing member itself plus every descendant, recursively."""
    descendant_ids = [cm_id]
    for child in get_sub_tms(session, cm_id):
        descendant_ids.extend(get_all_descendant_ids(session, child.cm_id))
    return descendant_ids


def aggregate_obligations(
    session: Session, cm_id: str, settlement_date: date
) -> dict:
    """Sum obligation net_value across a clearing member and all its sub-TMs."""
    cm_ids = get_all_descendant_ids(session, cm_id)
    obligations = (
        session.query(Obligation)
        .filter(
            Obligation.counterparty_id.in_(cm_ids),
            Obligation.settlement_date == settlement_date,
        )
        .all()
    )
    total_value = sum((Decimal(str(o.net_value)) for o in obligations), Decimal("0"))
    return {
        "cm_id": cm_id,
        "member_count": len(cm_ids),
        "obligation_count": len(obligations),
        "total_value": total_value,
    }
