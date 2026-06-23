# Trade Settlement Operations System

Deterministic post-trade clearing & settlement simulation covering NSE/BSE equity cash (T+1 standard, T+0 phased rollout for top 500 stocks), equity/currency/interest-rate derivatives, and corporate bond/G-Sec debt settlement. Enhanced with industry-standard innovations: ML-based fail prediction, CSDR progressive penalties, ISO 20022 messaging, counterparty risk scorecards, intraday liquidity monitoring, SPAN-style margining, multi-CM hierarchy, and SGF default waterfall simulation.

## Documentation

- **This README** — deep methodology for the original equity-cash pipeline: netting math, matching tolerances, break taxonomy, CSDR penalties, ML fail prediction, ISO 20022, scorecards, liquidity monitoring.
- **[`docs/TECHNICAL_REFERENCE.md`](docs/TECHNICAL_REFERENCE.md)** — full system architecture across all 6 product segments (all 21 pipeline steps), the complete data model, the module map, the design conventions every module follows, and an implementation guide for extending the system.
- **[`docs/NSE_CLEARING_SETTLEMENT_PLAN.md`](docs/NSE_CLEARING_SETTLEMENT_PLAN.md)** — the original phased build plan (Phase 1–5) this system was implemented against.

## Architecture

The system replicates the daily workflow of a settlements operations analyst through a 16-stage deterministic + agentic pipeline for equity cash, followed by 5 more steps (17–21) that seed and settle the other product segments — derivatives, margins, debt, and cross-segment advanced features. See [`docs/TECHNICAL_REFERENCE.md`](docs/TECHNICAL_REFERENCE.md) for the full 21-step picture; this section covers the original equity-cash 16:

1. **Trade Capture & Normalization** — Ingests from 3 source formats (OMS, broker confirmation, custodian statement), normalizes into a canonical trade schema
2. **Netting & Obligation Engine** — Multilateral netting at (ISIN, counterparty, settlement_date) grain with VWAP pricing, provisional + final stages
3. **SSI Golden-Copy Validation** — Pre-match validation of standing settlement instructions against a versioned reference table
4. **Matching Engine** — Two-way matching of internal vs counterparty net obligations with configurable price tolerance
5. **Custodian Confirmation** — Tracking confirmation cutoffs per settlement cycle, flagging late confirmations
6. **Settlement Instruction Generation** — SSI-enriched instructions for confirmed obligations
7. **ISO 20022 Message Formatting** — Structured XML messages in sese.023 format replacing legacy SWIFT MT540-543
8. **Break Detection & Classification** — Six-type taxonomy with cycle-aware escalation matrices
9. **CSDR Progressive Penalties** — Daily escalating cash penalties for settlement fails with counterparty billing
10. **Auction & Close-Out** — Short delivery resolution per NSE/BSE clearing rules
11. **ML Fail-Risk Prediction** — Gradient-boosted classifier (13 features) replacing simple heuristic scorer
12. **Agentic Triage Pipeline** — LangGraph dual-path: fail-risk prediction + break triage (RAG-assisted)
13. **Counterparty Risk Scorecards** — Composite scoring across 5 dimensions with letter grades and exposure limits
14. **Intraday Liquidity Monitor** — Real-time settlement flow tracking with programmable alerts
15. **Position Reconciliation** — EOD position derivation from settled obligations vs custodian holdings
16. **Reporting** — Multi-tab Excel + narrative DOCX with STP rate, cost-of-exception, counterparty analysis
17. **Dashboard** — Streamlit app with 10 tabs: breaks, analysis, ML risk, scorecards, penalties, liquidity, audit, recon, clearing members, risk & SGF

## Quick Start

```bash
pip install -r requirements.txt

# Generate synthetic data (~1,000 trades, 20 trading days)
python -m generators.synthetic_data

# Run the full pipeline (console logging)
SETTLE_LOG_FORMAT=console python -m main

# Launch the dashboard
streamlit run dashboard/app.py

# Run tests
pytest tests/ -v
```

## Production Deployment

### Docker

The system ships with a multi-stage Dockerfile (non-root user, slim base image) and docker-compose for full-stack deployment:

```bash
# Build and run pipeline → dashboard
docker compose up --build

# Run only the pipeline
docker compose run --rm pipeline

# Run tests in a container
docker compose run --rm test

# Tear down
docker compose down -v
```

The dashboard container includes health checks (`/_stcore/health`) compatible with Kubernetes liveness/readiness probes.

### Configuration (12-Factor)

All settings are sourced from environment variables with sensible defaults. Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|----------|---------|-------------|
| `SETTLE_DATABASE_URL` | `sqlite:///...` | Database connection string (SQLite or PostgreSQL) |
| `SETTLE_LOG_LEVEL` | `INFO` | Logging level |
| `SETTLE_LOG_FORMAT` | `json` | `json` for production, `console` for development |
| `SETTLE_ML_RISK_THRESHOLD` | `0.3` | ML fail probability threshold |
| `SETTLE_ENABLE_ML` | `true` | Toggle ML prediction module |
| `SETTLE_ENABLE_CSDR` | `true` | Toggle CSDR penalty module |
| `SETTLE_ENABLE_ISO20022` | `true` | Toggle ISO 20022 formatting |
| `SETTLE_ENABLE_DERIVATIVES` | `true` | Toggle derivatives settlement (step 18) |
| `SETTLE_ENABLE_MARGINS` | `true` | Toggle margin & collateral framework (step 19) |
| `SETTLE_ENABLE_DEBT` | `true` | Toggle debt & fixed income settlement (step 20) |
| `SETTLE_ENABLE_ADVANCED` | `true` | Toggle CM hierarchy/SGF/stress test/T+0/bond futures (step 21) |
| `SETTLE_DASHBOARD_PORT` | `8501` | Dashboard port |

See `.env.example` for the full list.

### Structured Logging

All pipeline output is structured JSON (structlog) in production — machine-parseable by ELK, Datadog, CloudWatch, or Loki:

```json
{"event": "step.complete", "step": 4, "name": "matching", "matched": 867, "breaks": 58, "timestamp": "2026-06-22T13:10:52Z"}
```

Set `SETTLE_LOG_FORMAT=console` for coloured, human-readable output during development.

### Resilience

- **Graceful shutdown**: The pipeline handles `SIGTERM`/`SIGINT` and drains cleanly between steps — safe for container orchestration
- **Circuit breaker + retry**: Knowledge base queries and ML model loading are wrapped with exponential back-off and a circuit breaker (configurable threshold/timeout)
- **Feature flags**: Each industry enhancement (ML, CSDR, ISO 20022, liquidity, scorecards) can be individually disabled without code changes

### CI/CD

GitHub Actions workflow (`.github/workflows/ci.yml`) runs on every push:
1. **Test** — pytest across Python 3.12 and 3.13
2. **Lint** — ruff static analysis
3. **Docker** — build verification for pipeline, dashboard, and test targets

### Makefile

```bash
make help          # Show available commands
make run           # Run pipeline (console logging)
make dashboard     # Launch Streamlit dashboard
make test          # Run test suite
make lint          # Run ruff linter
make docker-build  # Build all Docker images
make docker-up     # Run pipeline → dashboard
make clean         # Remove generated artifacts
```

## Project Structure

```
trade_settlement/
├── config/                     # YAML configuration (one file per concern)
│   ├── escalation_matrix.yaml  # Severity & aging thresholds + CSDR penalty rates
│   ├── matching_tolerances.yaml
│   ├── confirmation_cutoffs.yaml
│   ├── segment_settlement.yaml # Per-segment settlement cycle & cutoffs (Phase 1)
│   ├── derivatives_settlement.yaml  # Delivery margin ramp (Phase 2)
│   ├── margin_framework.yaml   # SPAN/exposure/VaR/cross-margin/limits/collateral (Phase 3)
│   ├── debt_settlement.yaml    # SGF issuer contribution rate (Phase 4)
│   └── t0_settlement.yaml      # T+0 trade/obligation cutoffs (Phase 5)
├── data/
│   ├── generated/              # Synthetic CSVs, SQLite DB, reports, ML model
│   └── knowledge_base/         # Break pattern corpus + FAISS index
├── docs/
│   ├── NSE_CLEARING_SETTLEMENT_PLAN.md  # Phase 1-5 build plan
│   └── TECHNICAL_REFERENCE.md  # Full system architecture + implementation guide
├── src/
│   ├── models/                 # SQLAlchemy schemas + enums (all 6 segments)
│   ├── ingestion/              # Trade capture & normalization (with boundary validation)
│   ├── netting/                # Obligation engine (VWAP, prov/final) — equity cash
│   ├── ssi/                    # SSI golden-copy validation
│   ├── matching/                # Two-way matching engine
│   ├── confirmation/             # Custodian confirmation tracking
│   ├── instruction/               # Settlement instruction + ISO 20022 formatter
│   ├── breaks/                     # Break detection rules engine
│   ├── penalties/                   # CSDR progressive cash penalty calculator
│   ├── auction/                      # Auction & close-out workflow
│   ├── triage/                        # LangGraph pipeline + FAISS KB + ML fail predictor
│   ├── risk/                           # Counterparty scorecard + portfolio stress test (Phase 5)
│   ├── liquidity/                       # Intraday liquidity monitor
│   ├── reconciliation/                   # EOD position reconciliation
│   ├── reporting/                         # Excel + DOCX report generation
│   ├── segments/                          # Per-segment config (Phase 1) + demo seed data
│   ├── derivatives/                        # MTM/premium/exercise/delivery/bond futures CTD (Phase 2+5)
│   ├── margins/                            # SPAN/exposure/VaR/delivery/cross margin/limits (Phase 3)
│   ├── collateral/                         # Collateral manager — haircuts, cash rule, concentration (Phase 3)
│   ├── debt/                               # Accrued interest, DvP-I, corp actions, G-Sec recon, SGF (Phase 4)
│   ├── cm_hierarchy/                       # TM-CM hierarchy + obligation aggregation (Phase 5)
│   ├── sgf/                                # SGF default waterfall simulation (Phase 5)
│   ├── settlement/                         # T+0 parallel settlement path (Phase 5)
│   ├── utils/                              # Config loader, resilience (retry + circuit breaker)
│   ├── settings.py             # 12-factor environment configuration
│   └── logging_config.py       # Structured logging (structlog JSON/console)
├── generators/                 # Synthetic data generator
├── tests/                      # pytest unit tests (430 tests)
├── dashboard/                  # Streamlit app (10 tabs)
├── .github/workflows/ci.yml   # GitHub Actions CI pipeline
├── Dockerfile                  # Multi-stage build (pipeline / dashboard / test)
├── docker-compose.yml          # Full-stack container orchestration
├── Makefile                    # Development & deployment commands
├── .env.example                # Environment variable template
├── main.py                     # 21-step pipeline orchestrator (equity cash + derivatives + margins + debt + advanced features)
└── requirements.txt
```

For the full breakdown of what each Phase 1–5 package does and why, see
[`docs/TECHNICAL_REFERENCE.md`](docs/TECHNICAL_REFERENCE.md).

---

## Methodology

### Netting Logic

The netting engine computes multilateral net obligations at the (ISIN, counterparty_id, settlement_date, exchange) grain. For each group, buy quantities are summed and sell quantities subtracted to produce a single net position:

- **Net buyer** (buy - sell > 0): `PAY_OUT` direction — the member receives securities and pays funds
- **Net seller** (buy - sell < 0): `PAY_IN` direction — the member delivers securities and receives funds
- **Zero net**: No obligation generated (positions cancel out)

**VWAP as a modeling simplification**: The net obligation carries a volume-weighted average price (VWAP) computed from its constituent trades. This is used by the matching engine to compare internal vs counterparty obligation prices with a configurable tolerance (default ±0.5%).

NSE Clearing actually computes net fund obligations as the sum of individual trade values (`Σ(price_i × quantity_i)`), not as `VWAP × net_quantity`. The VWAP approach produces a slightly different net fund value due to rounding and the interaction between netting and averaging. We chose VWAP because:
1. It makes the matching engine cleaner — a single price per obligation to compare
2. The difference is negligible for matching purposes (typically < 0.01%)
3. It aligns with how most institutional systems display net positions

**Two-stage obligations**: Obligations are computed twice:
- **Provisional** (end of T day): Based on all trades executed during the session
- **Final** (morning of T+1 for T+1 cycle, same-day for T+0): Incorporates any post-session corrections, cancellations, or corporate action adjustments

The gap between provisional and final is where most SSI-related breaks surface in real-world operations.

### Matching Logic

The matching engine performs a two-way match between internal (OMS-sourced) net obligations and counterparty (broker/custodian-sourced) net obligations. Match keys:

| Key | Comparison |
|-----|-----------|
| ISIN | Exact match |
| Net quantity | Exact match (zero tolerance) |
| VWAP price | Within ±0.5% (configurable) |
| Settlement date | Exact match |
| Counterparty ID | Exact match |
| Net direction | Exact match |

Match results:
- **MATCHED**: All keys match within tolerance → obligation proceeds to confirmation
- **BREAK**: Counterpart found but keys diverge → classified by type (quantity or price mismatch)
- **UNMATCHED**: No counterpart obligation found at all → remains pending

### Custodian Confirmation vs Settlement Instruction

These are distinct pipeline stages, not one step:

1. **Custodian confirmation**: The custodian (clearing member settling on behalf of FPIs, mutual funds, etc.) explicitly confirms it will settle the obligation. Until confirmed, the obligation cannot be instructed. Confirmation cutoffs:
   - T+1: 1:00 PM on settlement day (T+1)
   - T+0: 3:30 PM on trade day (market close)

2. **Settlement instruction**: After confirmation, the system generates an instruction with the counterparty's SSI fields (DP ID, settlement bank, account) for transmission to the depository/clearing bank.

Non-custodial obligations (broker-only) skip confirmation and go directly from MATCHED → CONFIRMED → INSTRUCTED.

### SSI Golden-Copy Approach

SSI mismatches are the #1 cause of settlement fails globally. The system maintains a separate, versioned SSI reference table rather than relying on per-trade SSI data:

- **Effective dating**: Each SSI record has `effective_from` and `effective_to` dates. When a counterparty changes SSI details, the old record is end-dated and a new record created.
- **Pre-match validation**: Every obligation's counterparty SSI is validated against the golden copy *before* the matching engine runs. SSI problems are flagged as `SSI_MISSING_OR_INCORRECT` breaks at the reference-data level, not the trade-data level.
- **Settlement instruction enrichment**: Confirmed obligations pull SSI fields from the golden copy when generating settlement instructions.

### Break Taxonomy

| Type | Trigger | Severity Basis |
|------|---------|---------------|
| `QUANTITY_MISMATCH` | Internal vs counterparty net qty differs | Value at risk |
| `PRICE_MISMATCH` | VWAP diff exceeds tolerance | Value at risk |
| `SSI_MISSING_OR_INCORRECT` | No active SSI or SSI field validation fails | Value at risk |
| `LATE_CONFIRMATION` | Custodian did not confirm by cutoff | Time past cutoff (not VAR) |
| `COUNTERPARTY_FAIL` | Short delivery — sell-side pay-in not delivered | Value at risk |
| `CORPORATE_ACTION_CONFLICT` | Settlement straddles a corporate action ex-date | Value at risk |

**LATE_CONFIRMATION rationale**: This is a timing-based break, not a data mismatch. Since no data is wrong — the confirmation is merely late — severity is based on how far past the cutoff the confirmation is, not on the obligation's monetary value. The thresholds (≤30 min = LOW, ≤2 hours = MEDIUM, >2 hours = HIGH) reflect the escalating fail risk as the settlement deadline approaches.

### Auction & Close-Out Mechanics

When a sell-side obligation is not delivered by settlement day:

1. **Valuation debit**: Closing price on the day preceding pay-in
2. **Buy-in auction**: Conducted on T+1 within a ±20% band around the reference price
3. **Auction settlement**: T+2 (the buyer does not receive shares until T+2 even in the auction scenario)
4. **Close-out**: If the auction sources no shares, cash settlement at the higher of:
   - Highest price from trade date to auction date
   - 20% above the official closing price on auction day

The auction/close-out outcome is persisted as a distinct status (`AUCTION` / `CLOSED_OUT`), separate from a regular break resolution.

### Escalation Matrix

**T+1 cycle (days-based):**

| Age | Min Severity | Escalation Level |
|-----|-------------|-----------------|
| 0-1 days | Per break value | 0 (normal) |
| 2-3 days | MEDIUM | 1 (escalated) |
| 4+ days | HIGH | 2 (critical) |

**T+0 cycle (hours-based):**

| Age | Min Severity | Escalation Level |
|-----|-------------|-----------------|
| 0-4 hours | Per break value | 0 |
| 4-8 hours | MEDIUM | 1 |
| 8+ hours | HIGH | 2 |

### Fail-Risk Prediction

The pre-settlement fail-risk scorer is a weighted heuristic model, not LLM reasoning:

| Factor | Weight | Scoring |
|--------|--------|---------|
| Counterparty historical fail rate | 30% | Normalized against 10% baseline |
| Settlement cycle | 20% | T+0 = 0.7, T+1 = 0.3 |
| Value concentration | 20% | Tiered by obligation value |
| Time pressure | 15% | Days to settlement deadline |
| Obligation status | 15% | PENDING riskier than INSTRUCTED |

The composite score (0-1) is classified into risk tiers: LOW (<0.4), MEDIUM (0.4-0.7), HIGH (≥0.7). High-risk obligations are surfaced in a priority queue for proactive ops intervention.

### Dual-Path Routing Rationale

The triage pipeline has two entry paths because they serve different operational purposes:

- **Path A** (fail-risk scan) runs on obligations that have *not* broken — it is a proactive, pre-settlement intervention tool. The output is a risk queue, not a break resolution.
- **Path B** (break triage) runs on obligations that *have* broken — it is a reactive, post-exception workflow ending in a human approval gate.

Running them as a single sequential pipeline would be incorrect: a PENDING obligation with a high fail-risk score should not go through the break classifier (it hasn't broken), and a BREAK obligation doesn't need fail-risk scoring (it already failed).

### STP Rate Calculation

STP (Straight-Through Processing) rate is defined as:

```
STP Rate = (obligations that reached SETTLED or INSTRUCTED with zero manual touches) / (total final-stage obligations) × 100
```

"Zero manual touches" means: no break detected, no SSI fix required, no late custodian confirmation, no auction. This is the industry-standard definition — it measures the percentage of obligations that flowed through the entire pipeline without human intervention.

### Audit-Logging Rationale

Every agentic decision (fail-risk score, break classification, root-cause investigation, resolution recommendation, escalation assessment) produces a persisted audit log entry containing:
- **Node name**: Which pipeline node made the decision
- **Inputs**: What data the node received
- **Conclusion**: What it decided
- **Rationale**: Why it decided that

This is not a debugging tool — it is a governance artifact. Financial institutions are moving toward requiring explainable reasoning chains for any agentic system that influences operational decisions. The audit log enables a human reviewer to trace any recommendation back to its inputs and understand the agent's reasoning.

### Where LLM Reasoning is Used vs Deliberately Excluded

**LLM/agentic reasoning is used for:**
- Root-cause investigation (RAG retrieval from knowledge base)
- Resolution recommendation drafting
- Natural-language report narrative generation

**LLM reasoning is deliberately excluded from:**
- Trade matching (deterministic, rule-based)
- Netting and obligation computation (arithmetic)
- Break detection and classification (rules engine)
- Severity and escalation assessment (config-driven thresholds)
- Fail-risk scoring (ML model — deterministic inference)
- Custodian confirmation tracking (deadline comparison)
- Auction/close-out calculations (formula-based)
- Position reconciliation (quantity comparison)
- CSDR penalty computation (formula-based)
- ISO 20022 message formatting (template-based)
- Counterparty risk scoring (weighted composite)
- Intraday liquidity monitoring (arithmetic)

The boundary is intentional: numeric comparison, matching, and financial calculations must be deterministic and auditable. LLM reasoning is reserved for tasks that require natural-language understanding, pattern recognition across unstructured precedents, or human-readable output generation.

---

## Industry Enhancements

### ML-Based Fail Prediction

Replaces the original 5-factor weighted heuristic scorer with a Gradient Boosted Classifier (sklearn GBM, 100 estimators, max depth 4) trained on 5,000 synthetic historical settlement records.

**13-dimensional feature vector:**

| # | Feature | Source |
|---|---------|--------|
| 1 | Counterparty 90-day fail rate | Rolling statistics |
| 2 | Counterparty fail count | Rolling statistics |
| 3 | Is T+0 settlement | Obligation |
| 4 | Log net obligation value | Obligation |
| 5 | Net quantity | Obligation |
| 6 | Days to settlement deadline | Calendar |
| 7 | Obligation status ordinal | Pipeline state |
| 8 | Security price level (volatility proxy) | Obligation |
| 9 | Counterparty type ordinal | Reference data |
| 10 | Hour of day | Clock (T+0 intraday) |
| 11 | Is month-end | Calendar |
| 12 | Concurrent obligations for counterparty | Pipeline state |
| 13 | ISIN-level historical fail rate | Rolling statistics |

The synthetic training data uses realistic distributions: ~3-5% overall fail rate, T+0 fails at ~2x T+1 rate, logistic label generation from a known feature-weight vector. The model outputs calibrated probabilities, not just risk tiers.

**Why GBM over the original heuristic**: The heuristic used fixed weights and step-function scoring for each factor independently. GBM captures feature interactions (e.g., high-value T+0 obligations from a counterparty with poor history compound risk non-linearly), handles continuous features without manual bucketing, and provides feature importance for explainability.

**Why not a neural network**: For 13 features and 5,000 training samples, GBM is the right tool. Neural networks would overfit and provide less interpretable feature importance. This aligns with industry practice — Accenture's production fail-prediction models use XGBoost/Random Forest for the same reasons.

### CSDR Progressive Penalties

Implements the Central Securities Depositories Regulation (EU 2022/1930, ESMA 70-156-5765) penalty framework adapted for Indian equity markets:

| Day | Rate (liquid) | Rate (illiquid) | Multiplier |
|-----|--------------|-----------------|------------|
| 1-3 | 1.0 bps/day | 0.5 bps/day | 1x |
| 4-7 | 2.0 bps/day | 1.0 bps/day | 2x |
| 8+ | 3.0 bps/day | 1.5 bps/day | 3x |

- Fails-to-deliver (PAY_IN direction) attract a 1.5x multiplier over fails-to-receive
- Penalties computed on settlement value, accruing daily from settlement date + 1
- Monthly aggregation per counterparty for billing
- Three penalty tiers: STANDARD (days 1-3), ESCALATED (days 4-7), CRITICAL (8+)

### ISO 20022 Settlement Messages

Generates structured XML messages in the `sese.023.001.09` (Securities Settlement Transaction Instruction) format, replacing legacy SWIFT MT540-543 messages per the SWIFT MT retirement timeline (Nov 2025).

Key field mappings:
- ISIN → `FinancialInstrumentIdentification`
- DP ID → `SafekeepingAccount`
- Settlement Bank → `CashSettlementParties` (mapped to BIC via lookup)
- Direction → `SecuritiesMovementType` (DELI/RECE)
- Depository → `PlaceOfSettlement` (NSDL: NSDLINBB, CDSL: CDSLINBB)

### Counterparty Risk Scorecard

Five-dimension composite risk scoring on a 0-100 scale:

| Dimension | Weight | Scoring |
|-----------|--------|---------|
| Settlement Efficiency | 25% | STP rate for the counterparty |
| Fail History | 25% | 90-day fail rate (0% = 100, 10%+ = 0) |
| Break Frequency | 20% | Breaks per 100 obligations |
| Timeliness | 15% | % of custodian confirmations before cutoff |
| Concentration Risk | 15% | Value-weighted Herfindahl index by ISIN |

Grades and exposure-limit multipliers:

| Grade | Score Range | Exposure Multiplier | Action |
|-------|-----------|-------------------|--------|
| A | 80-100 | 1.2x | Can increase exposure |
| B | 65-79 | 1.0x | Normal |
| C | 50-64 | 0.8x | Reduce exposure |
| D | 35-49 | 0.5x | Restrict new trading |
| F | 0-34 | 0.2x | Near-suspend |

Counterparties graded D or F are placed on a watch list.

### Intraday Liquidity Monitor

Real-time tracking of settlement flows per CPMI-IOSCO PFMI principles:

- **Liquidity snapshot**: Net fund position, gross pay-in/pay-out, buffer utilization
- **Settlement velocity**: Obligations settled per hour in rolling windows
- **Counterparty exposure**: Gross and net exposure per counterparty
- **Programmable alerts**: Buffer breach (70% warning, 90% critical), single-counterparty concentration (>30%), velocity drops (>50% decline), settlement deadline proximity

The liquidity buffer defaults to INR 50 crore — configurable per clearing member's actual collateral/margin with the clearing corporation.
