# Technical Reference & Implementation Guide

This document describes how the full system works end-to-end as of the
Phase 1–5 build-out (see [`NSE_CLEARING_SETTLEMENT_PLAN.md`](NSE_CLEARING_SETTLEMENT_PLAN.md))
and how to extend it. For the original equity-cash pipeline's detailed
methodology (netting math, matching tolerances, break taxonomy, CSDR
penalties, ML fail prediction, etc.), see [`README.md`](../README.md) — this
document does not repeat that material. It instead covers:

1. System architecture (all 21 pipeline steps, all 6 product segments)
2. Data model reference (every ORM table)
3. Module map (what lives where, and why)
4. Design conventions every module follows
5. Implementation guide — how to extend the system safely

---

## 1. System Architecture

The system simulates NSE Clearing & Settlement across six product segments:

| `ProductSegment` | Settlement cycle | Netting model |
|---|---|---|
| `EQUITY_CASH` | T+1 (T+0 parallel path) | Multilateral net (DvP-III) |
| `EQUITY_FO` | T+0 daily MTM, T+1 delivery | Per-contract, no netting across contracts |
| `CURRENCY_DERIV` | T+0 daily MTM | Per-contract |
| `IRD` | T+0 daily MTM | Per-contract |
| `DEBT_CORP_BOND` | T+1/T+2 | Gross, bilateral (DvP-I, no netting) |
| `DEBT_GSEC` | T+1 | Gross, bilateral (DvP-I, no netting); settles at CCIL, not NSE Clearing |

`main.py` is the single pipeline orchestrator (`run_pipeline()` →
`_run_equity_cash_pipeline()`). It runs 21 sequential steps against one
SQLite database (recreated fresh on every run). Steps 1–16 are the original
equity-cash DvP-III pipeline; steps 17–21 (added when Phase 2–5 modules were
wired in) seed and exercise the other five segments plus the cross-segment
"advanced features."

```
 1  trade_capture        ingest OMS/broker/custodian CSVs → canonical Trade rows
 2  netting              multilateral net obligations (provisional + final)
 3  ssi_validation       validate counterparty SSI against golden copy
 4  matching             internal vs counterparty obligation matching
 5  confirmation         custodian confirmation tracking vs cutoff
 6  instructions         settlement instruction generation
 7  iso20022             sese.023 XML message formatting          [flag: ENABLE_ISO20022]
 8  break_detection      break aging + severity/escalation update
 9  auction              short-delivery auction / close-out
10  csdr_penalties       CSDR progressive cash penalties           [flag: ENABLE_CSDR_PENALTIES]
11  ml_prediction        GBM fail-risk scoring                     [flag: ENABLE_ML_PREDICTION]
12  triage               LangGraph dual-path agentic triage + RAG
13  scorecards           counterparty risk scorecards              [flag: ENABLE_SCORECARDS]
14  liquidity            intraday liquidity monitor                [flag: ENABLE_LIQUIDITY_MONITOR]
15  reconciliation       EOD position reconciliation
16  reporting            Excel + DOCX report generation
17  segment_seed         seed derivatives/debt/collateral/CM/T0 fixtures (always runs)
18  derivatives          MTM, premium, exercise/assignment, final settlement, delivery   [flag: ENABLE_DERIVATIVES]
19  margins              SPAN, exposure, VaR, delivery margin, cross margin, limits, collateral checks [flag: ENABLE_MARGINS]
20  debt                 DvP-I settlement, accrued interest, corp actions, SGF, G-Sec/CCIL recon       [flag: ENABLE_DEBT]
21  advanced_features    CM hierarchy aggregation, SGF default waterfall, stress test, T+0, bond futures CTD [flag: ENABLE_ADVANCED_FEATURES]
```

Every step logs `step.start` / `step.complete` (or `step.skipped` if its
flag is off) via structlog, and `_check_shutdown(step)` is called after each
one so `SIGTERM`/`SIGINT` drains cleanly between steps.

### Why steps 17–21 exist separately from 1–16

Steps 1–16 ingest **real fixture data** committed under `data/generated/`
(`oms_trades.csv`, `broker_confirmations.csv`, etc.) — there's a genuine
multi-source reconciliation problem to simulate there. No equivalent fixture
files exist yet for F&O/currency/IRD/debt, so step 17
(`src/segments/demo_seed.py`) generates a small, fixed set of representative
ORM records directly (clearing members, derivative contracts/positions,
collateral, debt instruments/trades, T+0 equity trades) so steps 18–21 have
real DB rows to compute against, instead of those modules sitting unit-tested
but never exercised by the pipeline. This mirrors the synthetic-data
precedent already used for the equity-cash CSV fixtures — it isn't a new
pattern.

---

## 2. Data Model Reference

All tables live in `src/models/database.py`, declared against a single
SQLAlchemy `Base`. Enums live in `src/models/enums.py`.

| Table | Purpose | Key columns |
|---|---|---|
| `Trade` | Raw normalized trade (any segment, any source) | `trade_id`, `isin`, `settlement_cycle`, `product_segment` |
| `Obligation` | Net (or gross, for delivery legs) settlement obligation | `net_quantity`, `net_direction`, `vwap_price`, `obligation_stage`, `status` |
| `SSIRecord` | Versioned, effective-dated SSI golden copy | `effective_from`, `effective_to` |
| `BreakRecord` | A detected mismatch/exception | `break_type`, `severity`, `status`, `age_days` |
| `SettlementInstruction` | SSI-enriched instruction sent downstream | `direction`, `status` |
| `AuctionRecord` | Buy-in auction / close-out outcome | `auction_price`, `outcome` |
| `Counterparty` | Reference data for brokers/custodians/CCPs | `counterparty_type` |
| `CustodianHolding` | Custodian-reported holdings for recon | — |
| `AgenticAuditLog` | Persisted reasoning trace for every agentic decision | `node_name`, `inputs`, `conclusion`, `rationale` |
| `PositionRecord` | Derived EOD position from settled obligations | — |
| `DerivativeContract` | A listed F&O/currency/IRD contract | `contract_type`, `option_type`, `delivery_type`, `strike_price`, `lot_size`, `expiry_date` |
| `DerivativePosition` | An open position in a derivative contract | `buy_sell`, `quantity`, `trade_price`, `position_date` |
| `MTMSettlement` | A daily or final MTM cash flow | `settlement_price`, `mtm_amount` |
| `MarginRecord` | A margin requirement levied on a counterparty | `margin_type`, `amount`, `as_of_date` |
| `CollateralRecord` | Collateral pledged against margin | `collateral_type`, `value`, `haircut_pct` |
| `DebtInstrument` | A corporate bond / G-Sec reference record | `face_value`, `coupon_rate_pct`, `day_count_convention` |
| `DebtTrade` | A DvP-I (gross) debt trade | `securities_received`, `funds_received`, `status` |
| `ClearingMember` | A node in the TM-CM hierarchy | `cm_type`, `parent_cm_id` (self-referential), `net_worth`, `security_deposit` |

Nothing here uses foreign-key constraints — `counterparty_id` /
`cm_id` columns are plain strings reused across tables (e.g. the same
`BRK-001` row from `counterparty_master.csv` is reused as a `ClearingMember`,
a `DerivativePosition.counterparty_id`, and a `DebtTrade.buyer_id`). This
keeps the schema simple since the system is a settlement simulation, not a
system of record with referential-integrity requirements.

---

## 3. Module Map

```
src/
├── models/          ORM schema (database.py) + all enums (enums.py)
├── ingestion/        Trade capture & normalization (3 source formats, boundary validation)
├── netting/           Multilateral netting (VWAP, provisional/final) — equity cash
├── ssi/                SSI golden-copy validation
├── matching/            Two-way obligation matching
├── confirmation/         Custodian confirmation cutoff tracking
├── instruction/            Settlement instruction generation + ISO 20022 formatter
├── breaks/                  Break detection rules engine + aging/escalation
├── penalties/                 CSDR progressive cash penalty calculator
├── auction/                    Auction & close-out workflow
├── triage/                      LangGraph agentic pipeline, FAISS knowledge base, ML fail predictor
├── risk/                        Counterparty risk scorecard + portfolio stress testing (Phase 5)
├── liquidity/                    Intraday liquidity monitor
├── reconciliation/                EOD position reconciliation
├── reporting/                      Excel + DOCX report generation
├── segments/                       Per-segment settlement config (Phase 1) + demo seed data (Phase 5 wiring)
├── derivatives/                     MTM, premium, exercise/assignment, final settlement, physical delivery, bond futures CTD (Phase 2 + 5)
├── margins/                         SPAN, exposure margin, VaR margin, delivery margin, cross margin, position limits (Phase 3)
├── collateral/                      Collateral manager — haircuts, cash rule, concentration limit (Phase 3)
├── debt/                            Accrued interest, DvP-I settlement, corporate actions, trade ingestion, G-Sec/CCIL recon, SGF issuer contribution (Phase 4)
├── cm_hierarchy/                    TM-CM hierarchy registration + obligation aggregation (Phase 5)
├── sgf/                             SGF default waterfall simulation (Phase 5)
├── settlement/                      T+0 parallel settlement path (Phase 5)
└── utils/                           Config loader (YAML), resilience (retry + circuit breaker)
```

Each phase package is self-contained — `src/derivatives/` doesn't import
from `src/debt/`, etc. — except where explicitly noted (e.g.
`src/settlement/t0_engine.py` reuses `_compute_vwap`/`_net_trades` from
`src/netting/obligation_engine.py` rather than duplicating netting logic,
and `src/margins/delivery_margin.py` wraps
`src/derivatives/physical_delivery.compute_delivery_margin`).

---

## 4. Design Conventions

Every module in this codebase follows the same rules. Read this section
before adding new code — consistency matters more than local cleverness.

**No embedded market-data or pricing model.** Settlement prices (DSP/FSP),
margin inputs (option deltas/vegas, reference prices for stress testing),
bond quoted prices, and SGF resource pools are always parameters supplied by
the caller, never computed internally. This is deliberate: the system
simulates settlement *mechanics*, not market pricing. Search for "caller-
supplied" in module docstrings to find every instance of this boundary.

**Decimal arithmetic everywhere money is involved.** All monetary values use
`decimal.Decimal`, quantized to `Decimal("0.01")` (money) or
`Decimal("0.0001")` (prices/factors) with `ROUND_HALF_UP`. Never use `float`
for money — `Numeric` DB columns return `Decimal` via SQLAlchemy, and the
convention is to defensively re-wrap with `Decimal(str(x))` even when a
value is already a `Decimal`, since SQLite can return plain floats for
`Numeric` columns in some code paths.

**Config-driven policy parameters.** Anything that resembles an NSE-style
policy knob (price scan ranges, haircuts, bps rates, cutoff times) lives in
a YAML file under `config/`, loaded via a thin getter function in
`src/utils/config_loader.py` (`get_margin_framework_config()`,
`get_debt_settlement_config()`, etc.). Never hardcode a percentage or rate
directly in engine code — add it to the relevant YAML and read it through
the loader, matching the existing getter-per-file pattern.

**No LLM reasoning in computation.** Every settlement/margin/SGF/stress-
test/CTD calculation is deterministic and rule-based. LLM/agentic reasoning
is used *only* inside `src/triage/` (root-cause investigation via RAG,
resolution recommendation drafting, narrative report generation) — see
README §"Where LLM Reasoning is Used vs Deliberately Excluded" for the full
rationale. If you're tempted to have a model "decide" a margin amount or a
matching tolerance, don't — that boundary is intentional.

**Document known simplifications instead of hiding them.** Where a
calculation is a simplified approximation of the real NSE/SEBI methodology
(e.g. Act/Act day-count using a 365.25-day-year approximation instead of
full ICMA period-weighted Act/Act; the bond futures conversion factor
ignoring the next-coupon stub period), the module docstring says so
explicitly. Don't silently present an approximation as fully precise.

**Pure functions over DB-backed services where possible.** Modules like
`src/sgf/waterfall.py`, `src/risk/stress_test.py`, and
`src/derivatives/bond_futures.py` take dataclasses/dicts in and return
dataclasses/dicts out — they don't query the DB themselves except where they
genuinely need persisted state (e.g. `DerivativePosition` rows for stress
testing). This makes them trivially unit-testable without a DB fixture.

**In-memory SQLite fixture pattern for tests.** Every DB-touching test file
uses the same `@pytest.fixture` shape:

```python
@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
```

Copy this verbatim into new test files rather than inventing a variant.

---

## 5. Implementation Guide

### Running the system

```bash
pip install -r requirements.txt
SETTLE_LOG_FORMAT=console python -m main     # full 21-step pipeline
streamlit run dashboard/app.py                # 10-tab dashboard
pytest tests/ -v                              # 430 tests
```

Toggle any pipeline section without code changes via environment variables
(see `src/settings.py` for the full list, e.g. `SETTLE_ENABLE_DERIVATIVES`,
`SETTLE_ENABLE_MARGINS`, `SETTLE_ENABLE_DEBT`, `SETTLE_ENABLE_ADVANCED`).

### Adding a new computation to an existing segment

1. Add the pure-function module under the right package (e.g. a new margin
   type goes in `src/margins/`, not `src/derivatives/`).
2. If it needs a policy parameter, add it to the segment's YAML config and a
   getter in `config_loader.py` — don't hardcode it.
3. Write the module docstring stating whether it's deterministic/rule-based
   and what data (if any) must be caller-supplied vs DB-sourced.
4. Add a test file following the in-memory SQLite fixture pattern above.
5. If it should run as part of the pipeline, wire it into the relevant step
   in `main.py` (steps 18–21) and fold its result into that step's
   `logger.info("step.complete", ...)` call and, if notable, the final
   `pipeline.complete` summary line.

### Adding a brand-new product segment

1. Add the segment to `ProductSegment` in `src/models/enums.py`.
2. Add its settlement cycle/cutoffs to `config/segment_settlement.yaml` and
   confirm `src/segments/config.py`'s `get_segment_config()` resolves it.
3. Decide its netting model: multilateral net (mirror
   `src/netting/obligation_engine.py`) or gross/bilateral DvP-I (mirror
   `src/debt/corporate_bond_settlement.py`) — don't invent a third pattern
   without a concrete NSE methodology reason.
4. Add any new ORM tables it needs to `src/models/database.py`, following
   the existing column-naming and `SAEnum`/`Numeric`/`Date` conventions.
5. If the pipeline needs fixture data for it and none exists yet, extend
   `src/segments/demo_seed.py` rather than inventing a new seeding pattern.

### Extending the dashboard

`dashboard/app.py` uses `st.tabs([...])` with one `with tabN:` block per tab,
each in its own `# ── Tab N: Name ──` commented section. New tabs append to
the end of the tuple unpack and the `st.tabs([...])` list — don't reorder
existing tabs, since tab order is meaningful to users who've used the
dashboard before. There is currently no dashboard test coverage (verified
only via `ast.parse` syntax checks) — this is a known, accepted gap, not an
oversight to silently "fix" by adding a different kind of test without
discussing it first.

### Where to look first when something breaks

- **Pipeline step fails**: check `logger` JSON output for the `step` number
  and `name` — each step's exceptions surface with full tracebacks since
  there's no broad try/except around step bodies (intentional — failures
  should be loud, not swallowed).
- **A margin/SGF/stress number looks wrong**: these are pure functions —
  reproduce with the exact inputs in a throwaway script or test, not by
  re-reading the pipeline wiring. The bug is almost always in the formula or
  the config value, not in how `main.py` calls it.
- **A test passes locally but the pipeline run produces unexpected
  aggregates**: check whether the pipeline is operating on *real* fixture
  data (steps 1–16, `data/generated/*.csv`) vs *seeded* demo data (steps
  17–21, `src/segments/demo_seed.py`) — the two data sources are not
  reconciled with each other, by design (see §1 above).
