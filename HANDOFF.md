# Handoff — NSE Clearing & Settlement Simulation

## Goal

Build out a simulated NSE (National Stock Exchange of India) clearing and
settlement system in Python, following the phased plan in
[`docs/NSE_CLEARING_SETTLEMENT_PLAN.md`](docs/NSE_CLEARING_SETTLEMENT_PLAN.md).
The plan covers five segments — Equity Cash, Equity Derivatives (F&O),
Currency Derivatives, Interest Rate Derivatives (IRD), and Debt/Fixed Income —
plus cross-cutting concerns (margin framework, SGF, multi-CM hierarchy, risk).

**Standing instruction from the user**: implement each phase fully (code +
tests), then explicitly ask the user before starting the next phase. Do not
start unauthorized phases.

## Current Progress

**All 5 phases are complete.** 430 tests passing, zero regressions, last
verified with `python -m pytest -q` from the `trade_settlement/` directory.

| Phase | Status | Scope |
|---|---|---|
| 1 — Multi-Segment Foundation | ✅ Done | `ProductSegment` enum, segment-aware schema, `src/segments/` configs, `main.py` dispatch |
| 2 — Derivatives Settlement Engine | ✅ Done | `src/derivatives/`: MTM, premium settlement, exercise/assignment, final settlement, physical delivery |
| 3 — Margin Framework | ✅ Done | `src/margins/`: SPAN, exposure, VaR, delivery margin, cross margin, position limits, collateral manager |
| 4 — Debt & Fixed Income | ✅ Done | `src/debt/`: DvP-I gross settlement, day-count/accrued interest, corporate actions, CBRICS/RFQ/CCIL ingestion, read-only CCIL G-Sec reconciliation, issuer SGF contribution |
| 5 — Advanced Features | ✅ Done | CM hierarchy, SGF default waterfall, portfolio stress testing, T+0 parallel settlement, IRD bond futures CTD, dashboard tabs |

All work for Phase 5 (the most recent phase) was authorized by the user with
an explicit "Yes" before implementation began.

### Phase 5 deliverables (most recent work, this session)

- **Multi-CM hierarchy**: `CMType` enum (`TM_CM`, `SCM`, `PCM`) in
  [`src/models/enums.py`](src/models/enums.py); `ClearingMember` table
  (self-referential `parent_cm_id`) in
  [`src/models/database.py`](src/models/database.py);
  [`src/cm_hierarchy/hierarchy.py`](src/cm_hierarchy/hierarchy.py) registers
  members and recursively aggregates `Obligation.net_value` across a CM and
  all its sub-TMs.
- **SGF default waterfall**: [`src/sgf/waterfall.py`](src/sgf/waterfall.py)
  — 7-step cascade (defaulter margins/collateral → defaulter base capital →
  defaulter Core SGF → NSE Core SGF → pro-rata non-defaulting CM SGF → NSE
  other resources → insurance). Takes a `WaterfallInputs` dataclass of
  caller-supplied amounts; no embedded balance-sheet model.
- **Portfolio stress testing**: [`src/risk/stress_test.py`](src/risk/stress_test.py)
  — shocks a caller-supplied reference price against each `DerivativePosition`
  in the adverse direction, sums loss per counterparty, ranks top-N CMs by
  margin shortfall (margin held is also caller-supplied).
- **T+0 parallel settlement**: [`src/settlement/t0_engine.py`](src/settlement/t0_engine.py)
  + [`config/t0_settlement.yaml`](config/t0_settlement.yaml) — cutoff
  validation (`trade_cutoff` 13:30, `obligation_cutoff` 14:30) and same-day
  obligation netting for `SettlementCycle.T0` trades. Reuses
  `_compute_vwap`/`_net_trades` from `src/netting/obligation_engine.py`
  rather than duplicating the netting logic.
- **IRD bond futures CTD**: [`src/derivatives/bond_futures.py`](src/derivatives/bond_futures.py)
  — CBOT-style conversion factor approximation (semi-annual compounding,
  no stub-period adjustment — documented as a simplification), delivery
  basket construction, cheapest-to-deliver selection by minimum delivery cost.
- **Dashboard**: [`dashboard/app.py`](dashboard/app.py) gained two tabs —
  "Clearing Members" (hierarchy table + obligation aggregation by CM) and
  "Risk & SGF" (margin utilization table + interactive waterfall simulator
  with `st.number_input` fields feeding `run_default_waterfall` live).
- **Tests**: `tests/test_cm_hierarchy.py`, `tests/test_sgf_waterfall.py`,
  `tests/test_stress_test.py`, `tests/test_t0_engine.py`,
  `tests/test_bond_futures.py`, `tests/test_phase5_integration.py` (end-to-end
  composition test: T+0 settlement → CM aggregation → stress test → SGF
  waterfall → CTD selection, all on a shared in-memory DB).

## What Worked

- **Caller-supplied data pattern** (used in every phase): money/price/margin
  inputs are always passed in by the caller rather than computed from an
  embedded pricing or balance-sheet model (e.g. `reference_prices`,
  `margin_held`, `quoted_price` are all dict/dataclass parameters). This keeps
  modules deterministic, testable, and free of hidden assumptions about
  market data feeds.
- **Reusing existing private helpers across modules** instead of duplicating
  logic — e.g. `t0_engine.py` imports `_compute_vwap`/`_net_trades` from
  `obligation_engine.py` rather than re-implementing netting math. Same
  philosophy was used in Phase 4 (`gsec_integration.py` mirrors
  `position_recon.py`'s pattern) and Phase 2/3.
  - **Caveat**: this only works cleanly within the same codebase/team
    context; if a future module's needs diverge from the helper's exact
    behavior, copy-and-adapt is preferable to forcing a shared dependency.
- **Decimal + `ROUND_HALF_UP` quantized to `0.01`** for every money field,
  consistently, across all 5 phases — avoids float-precision bugs and keeps
  test assertions exact (e.g. `Decimal("1000.00")` rather than approximate
  comparisons).
- **Config-driven parameters via YAML** (`config/*.yaml` +
  `src/utils/config_loader.py` getter functions) for anything resembling a
  policy knob (cutoff times, bps rates, percentages) — keeps tests stable
  if NSE-style parameters change, and matches the existing pattern exactly.
- **Documenting known simplifications directly in module docstrings** rather
  than silently presenting approximations as fully accurate — e.g. Act/Act
  day-count is documented as a 365.25-day approximation (not full ICMA), and
  the bond futures conversion factor is documented as ignoring the
  next-coupon stub period. This was explicitly the right call per the user's
  "Simplicity First" + correctness-transparency guidance — don't build the
  full-precision version, but don't hide the gap either.
- **In-memory SQLite + `Base.metadata.create_all(engine)` + `@pytest.fixture`**
  pattern for every DB-touching test module — fast, isolated, zero setup
  cost, copy-pasteable across new test files.
- **Asking before each phase transition** — the user responded "Yes" each
  time before phase work began; this was explicitly requested as a standing
  instruction and should continue if more phases/features are added later.

## What Didn't Work / Pitfalls Hit

- **Stale Read-tool state after context compaction**: once, the Edit tool
  rejected a change to `src/models/enums.py` with "File has not been read
  yet" even though it had been read earlier in the (now-compacted)
  conversation. Fix: re-read the relevant lines immediately before retrying
  the edit. If this happens again, just re-`Read` the file (or the specific
  line range) right before editing — don't assume prior reads carry over
  perfectly across a compaction boundary.
- **Speculative imports**: briefly added `import uuid` to
  `src/debt/trade_ingestion.py` by copy-paste habit from a similar module,
  then had to remove it since `DebtTrade.trade_id` comes from the source
  record, not a generated UUID. Lesson: when copying patterns from a
  reference file (e.g. `normalizer.py`), don't carry over its imports
  wholesale — only what the new module actually uses.
- **No real attempt at full ICMA Act/Act day-count or full CBOT delivery
  basket logic with next-coupon stub adjustment** — these were deliberately
  scoped out as too complex relative to the value added, per the
  "Simplicity First" guideline. If higher day-count precision is ever
  needed, it would require tracking each bond's actual coupon schedule
  (next/previous coupon dates), which `DebtInstrument` currently doesn't
  store beyond `coupon_frequency`.

## Next Steps

Phase 5 was the last phase defined in
`docs/NSE_CLEARING_SETTLEMENT_PLAN.md` — **there is no Phase 6 currently
planned.** Before doing any further work, a fresh agent should:

1. **Ask the user** what they want next. Plausible directions (not yet
   authorized, do not start without confirmation):
   - Polish/productionize: wire the new Phase 5 modules into `main.py`'s
     segment-dispatch pipeline (currently the Phase 5 modules are standalone
     and unit-tested but not yet called from the main orchestration script,
     unlike some Phase 2–4 modules).
   - Extend `DebtInstrument` with next/previous coupon date fields if more
     precise Act/Act day-count or bond futures conversion factors are
     wanted later.
   - Add CRUD/seeding scripts for `ClearingMember` records (currently no
     seed data exists outside of tests — the dashboard's "Clearing Members"
     tab will show "No clearing members registered yet" on a fresh DB).
   - Performance/stress-volume testing of the new Phase 5 modules at scale
     (existing `tests/test_stress_volume.py` predates Phase 5 and doesn't
     cover it).
2. **Verify before trusting this document**: run
   `python -m pytest -q` from `trade_settlement/` and confirm the test count
   still matches "430 passed" (or has grown, if more work has landed since).
   If the count differs, treat this handoff as partially stale and re-derive
   current state from `git log` / the actual test output rather than this
   file's claims.
3. Continue following the "implement fully + ask before next phase/feature"
   standing instruction unless the user says otherwise.
