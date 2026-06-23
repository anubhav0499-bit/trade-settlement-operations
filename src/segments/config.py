"""Per-segment settlement configuration (§9 Phase 1 of the NSE clearing plan)."""

from dataclasses import dataclass

from src.models.enums import ProductSegment, SettlementCycle
from src.utils.config_loader import get_segment_settlement_config


@dataclass(frozen=True)
class SegmentConfig:
    product_segment: ProductSegment
    settlement_cycle: SettlementCycle
    provisional_cutoff: str | None
    final_cutoff: str | None


def get_segment_config(product_segment: ProductSegment) -> SegmentConfig:
    """Look up the settlement cycle and cutoffs configured for a segment."""
    raw = get_segment_settlement_config()
    entry = raw[product_segment.value]
    return SegmentConfig(
        product_segment=product_segment,
        settlement_cycle=SettlementCycle(entry["settlement_cycle"]),
        provisional_cutoff=entry["provisional_cutoff"],
        final_cutoff=entry["final_cutoff"],
    )
