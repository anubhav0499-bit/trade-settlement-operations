"""
Backtest harness — runs the settlement pipeline's core engines across a
simulated multi-day history to check structural integrity:

1. Conservation laws (netting nets to zero across a balanced market, the SGF
   waterfall fully accounts for the shortfall it's given, CM-hierarchy
   aggregation matches an independent re-sum).
2. Operational stability (no exceptions, no runtime blowup) as daily volume
   ramps and stress/default scenarios are injected.

No real historical market data exists in this repo (see data/generated/*.csv,
which are single-day fixtures) — see backtest/scenario.py for how the
synthetic multi-day history is generated.

Usage:
    python -m backtest.run_backtest [--days 20] [--base-volume 500] [--seed 2024]
"""

import argparse
import json
import time
from datetime import date, timedelta
from decimal import Decimal

import random

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base
from src.models.enums import ObligationStage, SourceSystem

from src.netting.obligation_engine import compute_obligations
from src.derivatives.mtm_engine import compute_daily_mtm
from src.margins.span_engine import compute_span_margin
from src.collateral.manager import check_concentration_limit
from src.cm_hierarchy.hierarchy import aggregate_obligations, get_all_descendant_ids
from src.sgf.waterfall import run_default_waterfall, WaterfallInputs, get_waterfall_summary

from backtest import scenario
from backtest import invariants


def _volume_multiplier(day_idx: int) -> float:
    """Cycles 1x-3x every 10 days, simulating realistic volume seasonality."""
    return 1.0 + 2.0 * abs(((day_idx % 10) - 5) / 5)


def run_backtest(num_days: int, base_volume: int, seed: int) -> tuple[list[dict], list[str]]:
    rng = random.Random(seed)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    start_date = date(2026, 1, 5)
    isin_prices = scenario.initial_isin_prices()

    cm_ids = scenario.seed_cm_hierarchy(session, count=8)
    scenario.seed_derivative_book(session, start_date, cm_ids)

    day_metrics = []
    all_violations = []

    for day_idx in range(num_days):
        trade_date = start_date + timedelta(days=day_idx)
        settle_date = trade_date + timedelta(days=1)

        is_default_day = day_idx > 0 and day_idx % 5 == 4
        is_stress_day = day_idx % 3 == 2
        daily_vol = 0.05 if is_stress_day else 0.015
        num_pairs = int(base_volume * _volume_multiplier(day_idx))

        day_start = time.time()
        day_violations = []

        try:
            # 1. Equity cash: matched trades -> netting conservation
            scenario.evolve_prices(isin_prices, daily_vol, rng)
            trade_count = scenario.generate_matched_trades(
                session, trade_date, settle_date, isin_prices, num_pairs, rng
            )
            # compute_obligations re-derives obligations from ALL trades ever
            # inserted (it has no date filter); scope to today's settlement
            # date before persisting, or every day re-inserts duplicate rows
            # for every prior day too.
            todays_obligations = [
                ob for ob in compute_obligations(session, SourceSystem.OMS, ObligationStage.FINAL)
                if ob.settlement_date == settle_date
            ]
            day_violations += invariants.check_netting_conservation(todays_obligations)
            for ob in todays_obligations:
                session.add(ob)
            session.commit()

            # 2. Derivatives MTM, chained day-over-day via prior MTMSettlement rows
            nifty_price = isin_prices[scenario.ISINS[0]] * Decimal("47")
            settlement_prices = {
                scenario.NIFTY_FUT: nifty_price,
                scenario.NIFTY_CE: (nifty_price * Decimal("0.0075")).quantize(Decimal("0.01")),
                scenario.RELIANCE_FUT: (isin_prices[scenario.ISINS[1]] * Decimal("2.7")).quantize(Decimal("0.01")),
            }
            compute_daily_mtm(session, trade_date, settlement_prices)

            # 3. SPAN margin for the two NIFTY-future-holding CMs
            for cm_id in cm_ids[:2]:
                span = compute_span_margin(session, cm_id, "NIFTY", nifty_price, is_index=True)
                day_violations += invariants.check_margin_nonnegative(
                    f"SPAN({cm_id})",
                    span.scenario_margin, span.short_option_minimum,
                    span.calendar_spread_charge, span.total_margin,
                )

            # 4. Collateral concentration — tilt one CM into violation on stress days
            violator = cm_ids[5] if is_stress_day else None
            collateral_records = scenario.seed_collateral(
                session, cm_ids, trade_date, concentration_violator=violator
            )
            for cm_id in cm_ids:
                cp_records = [r for r in collateral_records if r.counterparty_id == cm_id]
                conc_violations = check_concentration_limit(cp_records)
                day_violations += invariants.check_collateral_concentration(
                    cm_id, cp_records, conc_violations, expect_violation=(cm_id == violator)
                )

            # 5. CM hierarchy aggregation cross-check against an independent re-sum
            for parent_id in cm_ids[:2]:
                result = aggregate_obligations(session, parent_id, settle_date)
                descendant_ids = set(get_all_descendant_ids(session, parent_id))
                independent_sum = sum(
                    (Decimal(str(ob.net_value)) for ob in todays_obligations
                     if ob.counterparty_id in descendant_ids),
                    Decimal("0"),
                )
                day_violations += invariants.check_cm_aggregation(
                    parent_id, result["total_value"], independent_sum
                )

            # 6. SGF default waterfall on injected default days
            if is_default_day:
                shortfall = Decimal(str(rng.randint(500_000, 5_000_000)))
                inputs = WaterfallInputs(
                    defaulter_margin_collateral=Decimal(str(rng.randint(0, 2_000_000))),
                    defaulter_base_capital=Decimal(str(rng.randint(0, 1_000_000))),
                    defaulter_sgf_contribution=Decimal(str(rng.randint(0, 500_000))),
                    nse_sgf_contribution=Decimal(str(rng.randint(0, 500_000))),
                    other_cm_sgf_contributions={
                        cm: Decimal(str(rng.randint(0, 200_000))) for cm in cm_ids
                    },
                    nse_other_resources=Decimal(str(rng.randint(0, 1_000_000))),
                    insurance_cover=Decimal(str(rng.randint(0, 500_000))),
                )
                steps = run_default_waterfall(shortfall, inputs)
                summary = get_waterfall_summary(steps)
                day_violations += invariants.check_waterfall_conservation(shortfall, summary)

            error = None
        except Exception as exc:  # noqa: BLE001 - a day-level crash is itself the finding
            trade_count = 0
            todays_obligations = []
            error = f"{type(exc).__name__}: {exc}"

        elapsed = time.time() - day_start
        day_metrics.append({
            "day": day_idx,
            "date": trade_date.isoformat(),
            "trades": trade_count,
            "obligations": len(todays_obligations),
            "elapsed_s": round(elapsed, 4),
            "is_stress_day": is_stress_day,
            "is_default_day": is_default_day,
            "violations": day_violations,
            "error": error,
        })
        if error:
            all_violations.append(f"day {day_idx} ({trade_date}): CRASH — {error}")
        all_violations.extend(f"day {day_idx} ({trade_date}): {v}" for v in day_violations)

    session.close()
    return day_metrics, all_violations


def _print_report(day_metrics: list[dict], all_violations: list[str]) -> None:
    total_trades = sum(d["trades"] for d in day_metrics)
    total_obligations = sum(d["obligations"] for d in day_metrics)
    crashed_days = [d for d in day_metrics if d["error"]]
    elapsed_list = [d["elapsed_s"] for d in day_metrics if not d["error"]]

    print("=" * 72)
    print("BACKTEST REPORT")
    print("=" * 72)
    print(f"Days simulated:        {len(day_metrics)}")
    print(f"Total trades:          {total_trades}")
    print(f"Total obligations:     {total_obligations}")
    print(f"Days with exceptions:  {len(crashed_days)}")
    if elapsed_list:
        print(f"Per-day runtime:       min={min(elapsed_list):.3f}s  "
              f"max={max(elapsed_list):.3f}s  avg={sum(elapsed_list)/len(elapsed_list):.3f}s")
    print()
    print(f"Structural integrity violations: {len(all_violations)}")
    if all_violations:
        print("-" * 72)
        for v in all_violations:
            print(f"  [X] {v}")
    print("-" * 72)
    print("Per-day summary:")
    for d in day_metrics:
        flags = []
        if d["is_stress_day"]:
            flags.append("STRESS")
        if d["is_default_day"]:
            flags.append("DEFAULT")
        if d["error"]:
            flags.append("CRASHED")
        elif d["violations"]:
            flags.append(f"{len(d['violations'])} VIOLATION(S)")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"  day {d['day']:>2} ({d['date']}): {d['trades']:>5} trades, "
              f"{d['obligations']:>4} obligations, {d['elapsed_s']:.3f}s{flag_str}")
    print("=" * 72)
    verdict = "PASS" if not all_violations and not crashed_days else "FAIL"
    print(f"VERDICT: {verdict}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=20, help="number of simulated trading days")
    parser.add_argument("--base-volume", type=int, default=500, help="base matched-trade-pairs per day")
    parser.add_argument("--seed", type=int, default=2024, help="RNG seed for reproducibility")
    parser.add_argument("--json-out", type=str, default=None, help="optional path to write the full JSON report")
    args = parser.parse_args()

    day_metrics, all_violations = run_backtest(args.days, args.base_volume, args.seed)
    _print_report(day_metrics, all_violations)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"days": day_metrics, "violations": all_violations}, f, indent=2, default=str)
        print(f"Full report written to {args.json_out}")

    return 0 if not all_violations and not any(d["error"] for d in day_metrics) else 1


if __name__ == "__main__":
    raise SystemExit(main())
