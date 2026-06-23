"""
Settlement Guarantee Fund (SGF) Default Waterfall Simulation.

Simulates the 7-step cascade NSE Clearing applies a defaulting clearing
member's settlement shortfall against, in order:

1. Margins & collateral of the defaulting CM
2. Base capital / security deposit of the defaulting CM
3. Core SGF contribution of the defaulting CM
4. NSE Clearing's own contribution to Core SGF
5. Remaining Core SGF from non-defaulting CMs (pro-rata)
6. Any remaining NSE Clearing resources
7. Insurance (if procured)

All resource amounts are caller-supplied (no embedded balance-sheet model).
All computation is deterministic and rule-based — no LLM reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass
class WaterfallInputs:
    defaulter_margin_collateral: Decimal
    defaulter_base_capital: Decimal
    defaulter_sgf_contribution: Decimal
    nse_sgf_contribution: Decimal
    other_cm_sgf_contributions: dict[str, Decimal]
    nse_other_resources: Decimal
    insurance_cover: Decimal


@dataclass
class WaterfallStep:
    step_number: int
    step_name: str
    shortfall_before: Decimal
    applied: Decimal
    shortfall_after: Decimal


def run_default_waterfall(
    shortfall_amount: Decimal, inputs: WaterfallInputs
) -> list[WaterfallStep]:
    remaining = Decimal(str(shortfall_amount))
    steps: list[WaterfallStep] = []

    layers = [
        ("Defaulter margins & collateral", Decimal(str(inputs.defaulter_margin_collateral))),
        ("Defaulter base capital / security deposit", Decimal(str(inputs.defaulter_base_capital))),
        ("Defaulter Core SGF contribution", Decimal(str(inputs.defaulter_sgf_contribution))),
        ("NSE Clearing's Core SGF contribution", Decimal(str(inputs.nse_sgf_contribution))),
        (
            "Non-defaulting CMs' Core SGF (pro-rata)",
            sum((Decimal(str(v)) for v in inputs.other_cm_sgf_contributions.values()), Decimal("0")),
        ),
        ("NSE Clearing's remaining resources", Decimal(str(inputs.nse_other_resources))),
        ("Insurance", Decimal(str(inputs.insurance_cover))),
    ]

    for i, (name, available) in enumerate(layers, start=1):
        before = remaining
        applied = min(before, available).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        remaining = (before - applied).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        steps.append(WaterfallStep(i, name, before, applied, remaining))
        if remaining <= 0:
            break

    return steps


def get_waterfall_summary(steps: list[WaterfallStep]) -> dict:
    total_shortfall = steps[0].shortfall_before if steps else Decimal("0")
    total_covered = sum((s.applied for s in steps), Decimal("0"))
    final_shortfall = steps[-1].shortfall_after if steps else Decimal("0")
    return {
        "total_shortfall": total_shortfall,
        "total_covered": total_covered,
        "final_shortfall": final_shortfall,
        "fully_covered": final_shortfall <= 0,
        "steps_used": len(steps),
    }
