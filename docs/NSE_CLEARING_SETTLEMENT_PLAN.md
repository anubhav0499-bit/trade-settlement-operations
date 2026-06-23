# NSE Clearing & Settlement Architecture — Implementation Plan

> Planning document for extending the trade settlement system to cover NSE's
> full clearing and settlement structure across **Equity (Cash)**, **Equity
> Derivatives**, **Currency Derivatives**, **Interest Rate Derivatives**, and
> **Debt / Fixed Income** segments.

---

## Table of Contents

1. [Institutional Landscape](#1-institutional-landscape)
2. [Segment A — Equity Cash Market](#2-segment-a--equity-cash-market)
3. [Segment B — Equity Derivatives (F&O)](#3-segment-b--equity-derivatives-fo)
4. [Segment C — Currency Derivatives](#4-segment-c--currency-derivatives)
5. [Segment D — Interest Rate Derivatives](#5-segment-d--interest-rate-derivatives)
6. [Segment E — Debt / Fixed Income](#6-segment-e--debt--fixed-income)
7. [Cross-Cutting Concerns](#7-cross-cutting-concerns)
8. [Gap Analysis vs Current Codebase](#8-gap-analysis-vs-current-codebase)
9. [Implementation Phases](#9-implementation-phases)

---

## 1. Institutional Landscape

### 1.1 Key Entities

| Entity | Role |
|---|---|
| **NSE** (National Stock Exchange) | Trading venue — order matching across all segments |
| **NSE Clearing Ltd** (formerly NSCCL) | Central Counterparty (CCP) — novation, netting, settlement guarantee |
| **NSDL / CDSL** | Depositories — hold demat securities, effect DvP transfers |
| **Clearing Banks** (13 empanelled) | Fund settlement — maintain clearing accounts for CMs |
| **CCIL** (Clearing Corporation of India) | G-Sec settlement (NDS-OM), triparty repo, forex |
| **RBI** | Regulator for G-Sec, maintains SGL accounts |
| **SEBI** | Market regulator — prescribes margin framework, SGF norms, settlement cycles |

### 1.2 Clearing Member Hierarchy

```
NSE Clearing Ltd (CCP)
├── Trading Member – Clearing Member (TM-CM)
│     Can clear own trades + clients + other TMs + custodial participants
│     Net worth: ≥ ₹3 crore (equity), ≥ ₹10 crore (F&O)
│     Deposit: ₹50 lakh base + ₹10 lakh per additional TM
│
├── Self Clearing Member (SCM)
│     Can clear own proprietary + client trades only
│     Net worth: ≥ ₹1 crore
│
├── Professional Clearing Member (PCM)
│     Not a TM — typically banks/custodians
│     Clears for TMs and custodial participants
│     Net worth: ≥ ₹3 crore (equity), ≥ ₹10 crore (F&O)
│
└── Custodial Participant (CP)
      FPIs/institutions — trades cleared through a CM
```

### 1.3 Settlement Guarantee Fund (SGF) & Default Waterfall

**Core SGF** is maintained per segment. Minimum corpus = stress-test coverage for
top 2 members' worst-case credit exposure.

**Default waterfall** (in order):

1. Margins & collateral of the defaulting CM
2. Base capital / security deposit of the defaulting CM
3. Core SGF contribution of the defaulting CM
4. NSE Clearing's contribution to Core SGF (min 50% of MRC)
5. Remaining Core SGF from non-defaulting CMs (pro-rata risk-based)
6. Any remaining NSE Clearing resources
7. Insurance (if procured)

**Collateral composition rule:** ≥ 50% of effective deposits must be cash.

---

## 2. Segment A — Equity Cash Market

### 2.1 Settlement Cycle

| Cycle | Status | Cutoffs |
|---|---|---|
| **T+1** | Mandatory for all scrips since Jan 2023 | Provisional obligations: 9:00 PM on T day; Final obligations: 9:00 AM on T+1 |
| **T+0** (beta) | Optional — top 500 stocks since phased rollout Jan 2025 | Trades until 1:30 PM; obligations by 2:30 PM; settlement same day |

### 2.2 Obligation Determination

- **Multilateral netting** at (ISIN, CM, settlement date) grain
- Separate pay-in/pay-out for funds and securities
- Net obligation = Σ(buy qty) − Σ(sell qty) per netting key
- Net fund obligation = Σ(individual trade values), NOT VWAP × net qty
  (NSE Clearing computes on gross trade value basis)

### 2.3 Securities Settlement

- Securities pay-in/pay-out via NSDL and CDSL
- **Earmarking system** (since Nov 2022): shares earmarked, not debited from
  client demat until settlement — eliminates broker misuse
- DvP-III basis for institutional trades

### 2.4 Funds Settlement

- Via 13 empanelled clearing banks
- CM maintains clearing account with clearing bank
- Early pay-in incentive: margin benefit for securities delivered before pay-in

### 2.5 Short Delivery Handling

```
T+1 (settlement day)
  └── Short delivery detected
        └── Buy-in auction conducted on T+1 itself
              ├── Auction successful → settled on T+2
              └── Auction fails → Close-out
                    └── Highest price from trade day to auction day
                        OR 20% above closing price on auction day
                        (whichever is higher)
```

- **Valuation debit**: member debited at closing price on the day preceding pay-in
- **Penalty**: annualized penalty on short delivery value (SEBI circular rates)

### 2.6 Margin Framework (Cash Segment)

| Margin | Calculation |
|---|---|
| **VaR Margin** | 99% VaR, 1-day horizon, computed per scrip using EWMA/GARCH |
| **ELM** (Extreme Loss Margin) | Fixed % of gross position (typically 3.5–5% depending on scrip) |
| **Mark-to-Market** | Intraday MTM loss on open positions |
| **Delivery Margin** | Additional margin on T+1 for physical delivery trades |

### 2.7 What Our System Already Covers (Equity)

- ✅ T+1 netting and obligation computation
- ✅ Three-source matching (OMS, broker, custodian)
- ✅ SSI validation
- ✅ Confirmation workflow (custodian-facing)
- ✅ Break detection, aging, escalation (T+0 and T+1)
- ✅ Settlement instruction generation (ISO 20022 sese.023)
- ✅ Short delivery detection → auction initiation → close-out
- ✅ CSDR-style progressive penalties
- ✅ Position reconciliation (internal vs custodian)
- ✅ ML fail prediction, counterparty scorecard, liquidity monitoring

### 2.8 Gaps to Fill (Equity)

- ❌ T+0 settlement cycle as a parallel path (obligation cutoffs at 2:30 PM)
- ❌ Earmarking model (pre-settlement share blocking at depository)
- ❌ Real NSE Clearing obligation file parsing (actual MIS report formats)
- ❌ Clearing bank fund settlement integration
- ❌ VaR margin computation (EWMA model per scrip)
- ❌ Early pay-in incentive tracking
- ❌ Multi-CM hierarchy (TM-CM clearing for sub-TMs)

---

## 3. Segment B — Equity Derivatives (F&O)

### 3.1 Product Universe

| Product | Settlement | Expiry |
|---|---|---|
| Index Futures (NIFTY, BANKNIFTY, etc.) | Cash settled — daily MTM + final | Weekly / Monthly |
| Index Options | Cash settled — premium daily + exercise on expiry | Weekly / Monthly |
| Stock Futures | **Physically settled** since Oct 2019 | Monthly |
| Stock Options | **Physically settled** since Oct 2019 | Monthly |

### 3.2 Daily Settlement Flows

#### A. Mark-to-Market (Futures)

- All open futures positions marked to daily settlement price (DSP) at EOD
- DSP = volume-weighted average price of last 30 mins of trading
- Profit/loss computed: (DSP − previous DSP or trade price) × lot size × qty
- Cash settled via clearing bank on **T+1** basis
- **T+0 MTM option**: CMs can opt (quarterly) to settle MTM same day — if opted,
  scaled-up margin is NOT levied

#### B. Premium Settlement (Options)

- Net premium payable/receivable computed at client level across all option
  contracts per CM
- Premium settlement on **T+1** basis
- Premium is the ONLY cash flow for option buyers until exercise/expiry

#### C. Final Settlement (Futures Expiry)

- All open positions marked to Final Settlement Price (FSP)
- **Index futures FSP** = volume-weighted average of underlying index in last 30 mins
- **Stock futures** = physically settled (delivery of underlying shares)
- Cash component settled on T+1 (T = expiry day)

#### D. Exercise Settlement (Options Expiry)

- **European style** — exercise only at expiry (automatic for ITM)
- **Index options**: cash settled at FSP on T+1
- **Stock options**: physically settled — ITM positions result in delivery obligation
- Random assignment of short positions to meet long exercise
- Physical delivery obligation determined on expiry day, settled on T+1

### 3.3 Physical Delivery Mechanism (Stock F&O)

```
Expiry Day (T)
  ├── ITM stock options → delivery obligation generated
  ├── Expiring stock futures → delivery obligation generated
  │
  T+1 (Settlement Day)
  ├── Sellers deliver shares to NSE Clearing pool account
  ├── Buyers deliver funds to clearing bank
  └── NSE Clearing effects DvP simultaneously
```

- **Delivery margin** levied from E-4 days before expiry (incremental, reaching
  VaR + ELM equivalent by expiry)
- **Don't Exercise (DNE)** facility: long ITM option holders can choose not to
  exercise (must submit before cutoff)

### 3.4 Margin Framework (Derivatives)

| Margin Type | Description |
|---|---|
| **SPAN Margin** | Portfolio-based (Standard Portfolio Analysis of Risk); 99% VaR, 1-day or 2-day horizon; considers 16 risk scenarios (price scan range × volatility scan range) |
| **Exposure Margin** | Additional buffer on top of SPAN; 3% for index, 5%/1.5×σ for stocks |
| **Premium Margin** | Net option premium payable by buyer |
| **Assignment Margin** | Levied on assigned short option positions until exercise settlement |
| **Delivery Margin** | Incremental margin from E-4 to expiry for physical delivery contracts |
| **Additional Margin** | SEBI discretionary — imposed during volatile periods |
| **Extreme Loss Margin (ELM)** | 1% of gross open positions, real-time |
| **Cross Margin** | Benefit for hedged positions across correlated contracts (e.g., long futures + short call) |

#### SPAN Computation Parameters

- **Price Scan Range**: 3σ for index, 3.5σ for stocks (with minimum floors)
- **Volatility Scan Range**: 25% of price scan range
- **Calendar Spread Charge**: margin for inter-month spread positions
- **Short Option Minimum**: floor margin for deep OTM short options
- **Net Option Value**: credit for net long options in portfolio

### 3.5 Gaps to Fill (Derivatives)

- ❌ Daily MTM settlement engine (separate from cash segment netting)
- ❌ Options premium settlement (net premium payable/receivable per CM)
- ❌ SPAN margin computation (16 risk scenarios, portfolio-level)
- ❌ Exposure margin, delivery margin, cross margin
- ❌ Physical delivery obligation generation (stock F&O on expiry)
- ❌ Exercise and assignment engine (random assignment for short options)
- ❌ Final settlement price computation (VWAP of last 30 mins of underlying)
- ❌ Futures contract roll tracking
- ❌ Position limits enforcement (market-wide + CM-level + client-level)
- ❌ T+0 MTM opt-in mechanism (quarterly election)

---

## 4. Segment C — Currency Derivatives

### 4.1 Product Universe

| Product | Pairs | Settlement |
|---|---|---|
| Currency Futures | USD/INR, EUR/INR, GBP/INR, JPY/INR | Cash settled only (FEMA compliance) |
| Currency Options | USD/INR, EUR/INR, GBP/INR, JPY/INR | Cash settled — European style |
| Cross-Currency Futures | EUR/USD, GBP/USD, USD/JPY | Cash settled in INR |

### 4.2 Settlement Mechanism

- **Daily MTM**: identical to equity derivatives — DSP at EOD, settled T+1
- **Premium settlement**: net premium per CM per day, settled T+1
- **Final settlement (futures)**: FSP = RBI reference rate on expiry day, settled T+1
- **Exercise settlement (options)**: European, automatic ITM exercise, cash settled T+1
- **No physical delivery** of foreign currency — all cash settled in INR

### 4.3 Key Differences from Equity F&O

- Settlement price = RBI reference rate (not VWAP of exchange trading)
- Position limits tied to FEMA regulations and RBI guidelines
- Lot sizes smaller, margins typically lower due to lower volatility
- No physical delivery — eliminates delivery obligation complexity

### 4.4 Gaps to Fill (Currency Derivatives)

- ❌ RBI reference rate integration for settlement price
- ❌ Currency-specific position limit rules (FEMA compliance)
- ❌ Cross-currency pair handling (settlement in INR for non-INR pairs)
- ❌ Currency margin parameters (different scan ranges from equity)

---

## 5. Segment D — Interest Rate Derivatives

### 5.1 Product Universe

| Product | Underlying | Settlement |
|---|---|---|
| Treasury Bill Futures | 91-day T-Bill | Cash settled |
| Government Bond Futures | 6yr/10yr/13yr G-Sec | **Physically settled** (delivery of eligible G-Sec) |
| MIBOR Overnight Futures | Mumbai Interbank Offered Rate | Cash settled |

### 5.2 Settlement Mechanism

- **Daily MTM**: same framework as equity/currency derivatives, T+1
- **Final settlement (T-Bill / MIBOR futures)**: cash settled against reference rate
- **G-Sec bond futures**: physically settled via RBI SGL/CSGL accounts
  - Delivery basket: list of eligible G-Sec bonds determined by NSE Clearing
  - Conversion factor applied to standardize delivery across different coupons/maturities
  - Cheapest-to-deliver (CTD) bond economics apply
- **Settlement price**: computed using MIBOR OIS rates for interpolation of
  theoretical futures prices

### 5.3 Gaps to Fill (IRD)

- ❌ Bond futures delivery basket and conversion factor computation
- ❌ CTD bond identification
- ❌ RBI SGL account settlement integration
- ❌ MIBOR reference rate integration
- ❌ OIS curve interpolation for theoretical pricing

---

## 6. Segment E — Debt / Fixed Income

### 6.1 Sub-Segments

| Sub-Segment | Platform | Clearing Entity | Settlement Basis |
|---|---|---|---|
| **Government Securities** | NDS-OM (RBI platform) | CCIL (not NSE Clearing) | DvP-III (net), T+1 |
| **Corporate Bonds** | CBRICS / RFQ (NSE platforms) | NSE Clearing | DvP-I (gross), T+1 or T+2 |
| **Triparty Repo** | TREPS (CCIL platform) | CCIL | Same day / T+1 |
| **Negotiated Trades** | NSE Debt Trading Platform | NSE Clearing | Bilateral, various |

### 6.2 Corporate Bond Settlement (NSE Clearing)

```
Trade Reporting (CBRICS / RFQ / CCIL)
  │
  ▼
NSE Clearing accepts trade for settlement
  │
  ▼
Settlement Date
  ├── Seller: transfers bonds to NSE Clearing's DP account
  ├── Buyer: transfers funds to NSE Clearing's bank account
  │
  ▼
NSE Clearing verifies both legs (DvP-I)
  ├── Both received → Pay-out: bonds to buyer, funds to seller
  └── Either missing → Settlement failure / penalty
```

- **DvP-I basis**: gross for both securities AND funds (no netting)
- Settled at **participant level** (not CM level)
- Eligible: all demat corporate bonds reported on CBRICS, RFQ, or CCIL

### 6.3 Government Securities (CCIL — not NSE Clearing)

- Traded on NDS-OM (order matching) or OTC (reported to CCIL)
- CCIL acts as CCP — settlement guarantee via SGF
- **DvP-III**: multilateral net settlement for both securities and funds
- Settlement via RBI SGL accounts (securities) and current accounts (funds)
- **Repo settlement**: opening leg T+0 or T+1, closing leg as per tenor
- **Triparty repo (TREPS)**: standardized overnight/term repo, CCIL as CCP,
  securities held in CCIL's triparty repo gilt account

### 6.4 NSE Clearing Core SGF — Debt Segment Specifics

- Issuer contribution: 0.5 bps of issuance value per annum (upfront, based on maturity)
- CM contribution: risk-based pro-rata to fill deficit in MRC post issuer contribution

### 6.5 Gaps to Fill (Debt)

- ❌ Corporate bond DvP-I gross settlement engine
- ❌ CBRICS/RFQ trade report ingestion
- ❌ Debt-specific clearing member framework
- ❌ Accrued interest computation (clean price + accrued = dirty price)
- ❌ Corporate action handling for debt (coupon payments, call/put options, maturity)
- ❌ CCIL integration for G-Sec and triparty repo (separate CCP)
- ❌ NDS-OM trade feed integration
- ❌ Day-count convention handling (30/360, Actual/365, Actual/Actual)

---

## 7. Cross-Cutting Concerns

### 7.1 Collateral Management

| Item | Detail |
|---|---|
| Acceptable collateral | Cash, bank guarantees, FDRs, approved securities (G-Sec, equity with haircuts), approved bullion |
| Cash requirement | ≥ 50% of effective deposits must be cash |
| Haircuts | Security-specific, reviewed periodically; G-Sec: 2–10%, equities: 15–50% based on liquidity |
| Concentration limits | Single security ≤ 10% of total collateral, single issuer limits apply |
| Additional Base Capital (ABC) | CMs provide ABC for TMs needing extra trading capacity; same collateral forms |

### 7.2 Risk Management — Real-Time

- **Intraday margin monitoring**: positions checked every 30 minutes (at minimum)
- **Auto square-off**: triggered if CM margin shortfall exceeds threshold
- **Market-wide position limits**: per underlying (e.g., 20% of free float for stock F&O)
- **Stress testing**: daily stress tests for top N CMs, weekly full portfolio stress
- **Concentration margin**: additional margin when CM holds large % of OI

### 7.3 Penalty Framework (All Segments)

| Violation | Penalty |
|---|---|
| Short delivery (equity) | Valuation debit + buy-in auction; close-out at highest price or 20% above close |
| Fund shortage | Interest on shortfall + penal charges; ≥ ₹5 lakh → possible suspension |
| Margin shortfall | Interest @ 0.07% per day on shortfall amount |
| Late confirmation | Time-based escalation (30 min → MEDIUM, 2 hr → HIGH) |
| Derivatives delivery default | 3% of settlement price + replacement cost |

### 7.4 Reporting & Regulatory

- Daily obligation reports (MIS) per CM
- Daily margin reports (SPAN, exposure, delivery)
- Position limit reports (client, CM, market-wide)
- Settlement guarantee fund utilization reports
- SEBI quarterly reporting on SGF adequacy
- Monthly penalty aggregation reports by counterparty

---

## 8. Gap Analysis vs Current Codebase

### What We Have (Equity Cash — 16-stage pipeline)

| Stage | Module | Status |
|---|---|---|
| Data Ingestion | `src/ingestion/normalizer.py` | ✅ OMS, broker, custodian |
| Netting | `src/netting/obligation_engine.py` | ✅ Multilateral netting |
| SSI Validation | `src/ssi/golden_copy.py` | ✅ Effective-dated SSI |
| Matching | `src/matching/engine.py` | ✅ Two-way match with tolerance |
| Confirmation | `src/confirmation/cutoff_engine.py` | ✅ Custodian confirmation workflow |
| Break Detection | `src/breaks/rules_engine.py` | ✅ 6 break types, T+0/T+1 aging |
| Triage (LLM) | `src/triage/` | ✅ Root cause + resolution recommendation |
| Instruction Gen | `src/instruction/iso20022_formatter.py` | ✅ sese.023 XML |
| Auction | `src/auction/buy_in_engine.py` | ✅ Short delivery → auction → close-out |
| Penalties | `src/penalties/csdr_penalties.py` | ✅ Progressive daily penalties |
| Reconciliation | `src/reconciliation/position_recon.py` | ✅ Internal vs custodian |
| ML Risk | `src/triage/ml_fail_predictor.py` | ✅ 13-feature GBM model |
| Scorecard | `src/risk/counterparty_scorecard.py` | ✅ 5-dimension composite |
| Liquidity | `src/liquidity/intraday_monitor.py` | ✅ Snapshots, alerts, velocity |

### What We Need to Add

| Priority | Module | Segment | Complexity |
|---|---|---|---|
| **P0** | Multi-segment trade model (enums, DB schema) | All | Medium |
| **P0** | Derivatives MTM settlement engine | Equity F&O, Currency, IRD | High |
| **P0** | Options premium settlement | Equity F&O, Currency | Medium |
| **P1** | SPAN margin computation | Equity F&O, Currency, IRD | Very High |
| **P1** | Physical delivery obligation engine | Stock F&O, Bond Futures | High |
| **P1** | Exercise & assignment engine | Options (all segments) | High |
| **P1** | Corporate bond DvP-I settlement | Debt | Medium |
| **P2** | VaR margin model (EWMA/GARCH) | Equity Cash | High |
| **P2** | Collateral management & haircuts | All | High |
| **P2** | Exposure, delivery, cross margin | F&O, Currency | Medium |
| **P2** | Position limits enforcement | F&O, Currency, IRD | Medium |
| **P3** | T+0 parallel settlement path | Equity Cash | Medium |
| **P3** | Bond futures delivery basket & CTD | IRD | High |
| **P3** | Accrued interest & day-count conventions | Debt | Medium |
| **P3** | CCIL integration (G-Sec, triparty repo) | Debt | High |
| **P3** | Default waterfall simulation | All | Medium |
| **P3** | Multi-CM hierarchy (TM-CM → sub-TMs) | All | Medium |

---

## 9. Implementation Phases

### Phase 1 — Multi-Segment Foundation (est. 2–3 weeks)

**Goal**: extend the data model and pipeline to support multiple NSE segments
without breaking the existing equity cash pipeline.

- [ ] Extend `enums.py`: add `Segment` values (EQUITY_CASH, EQUITY_FO,
      CURRENCY_DERIV, IRD, DEBT_CORP_BOND, DEBT_GSEC), add derivative-specific
      enums (ContractType, ExerciseStyle, DeliveryType, OptionType, etc.)
- [ ] Extend `database.py`: add DerivativeContract, DerivativePosition,
      MarginRecord, CollateralRecord, MTMSettlement tables
- [ ] Create `src/segments/` package with segment-specific configs
- [ ] Refactor `main.py` pipeline to dispatch by segment
- [ ] Add segment-aware settlement cycle configuration (T+0, T+1, T+2)
- [ ] Tests: ensure all 274 existing tests still pass

### Phase 2 — Derivatives Settlement Engine (est. 3–4 weeks)

**Goal**: daily MTM, premium settlement, and final settlement for equity F&O
and currency derivatives.

- [ ] `src/derivatives/mtm_engine.py` — daily mark-to-market computation
      (DSP lookup, P/L per position, multilateral netting of MTM obligations)
- [ ] `src/derivatives/premium_engine.py` — net premium payable/receivable
      per CM at client level
- [ ] `src/derivatives/final_settlement.py` — expiry-day final settlement
      (FSP computation, position closeout)
- [ ] `src/derivatives/exercise_engine.py` — automatic ITM exercise,
      random assignment to short positions, DNE handling
- [ ] `src/derivatives/physical_delivery.py` — stock F&O delivery obligation
      generation, delivery margin computation (E-4 to expiry)
- [ ] Currency derivatives: settlement price = RBI reference rate integration
- [ ] Settlement reports: daily MTM report, premium report, delivery obligation report
- [ ] Tests: MTM arithmetic, premium netting, exercise assignment, physical delivery

### Phase 3 — Margin Framework (est. 3–4 weeks)

**Goal**: SPAN margin computation, exposure margin, and collateral management.

- [ ] `src/margins/span_engine.py` — NSCCL-SPAN implementation
      (16 risk scenarios, price scan range, volatility scan range, calendar
      spread charge, short option minimum, net option value)
- [ ] `src/margins/exposure_margin.py` — additional margin on top of SPAN
      (3% index, 5%/1.5σ stocks)
- [ ] `src/margins/var_model.py` — EWMA volatility model for VaR margin
      (cash segment)
- [ ] `src/margins/delivery_margin.py` — incremental delivery margin E-4 to expiry
- [ ] `src/margins/cross_margin.py` — hedged position benefit
- [ ] `src/collateral/manager.py` — collateral valuation, haircuts,
      concentration limits, 50% cash rule, ABC tracking
- [ ] `src/margins/position_limits.py` — market-wide, CM-level, client-level
      position limit checks
- [ ] Tests: SPAN scenarios, margin adequacy, collateral haircuts, position limits

### Phase 4 — Debt & Fixed Income (est. 2–3 weeks)

**Goal**: corporate bond DvP-I settlement, accrued interest, and CCIL awareness.

- [ ] `src/debt/corporate_bond_settlement.py` — DvP-I gross settlement
      (no netting), participant-level settlement
- [ ] `src/debt/trade_ingestion.py` — CBRICS/RFQ/CCIL trade report parsing
- [ ] `src/debt/accrued_interest.py` — day-count conventions (30/360,
      Act/365, Act/Act), clean→dirty price
- [ ] `src/debt/corporate_actions.py` — coupon payments, call/put exercise,
      maturity redemption
- [ ] `src/debt/gsec_integration.py` — CCIL settlement interface (read-only
      position reconciliation; actual G-Sec settlement is CCIL's domain)
- [ ] Debt-specific SGF contribution computation (0.5 bps of issuance value)
- [ ] Tests: DvP-I settlement, accrued interest, day-count, corporate actions

### Phase 5 — Advanced Features (est. 2–3 weeks)

**Goal**: complete the production-grade clearing system.

- [ ] `src/sgf/waterfall.py` — default waterfall simulation (7-step cascade)
- [ ] `src/risk/stress_test.py` — portfolio stress testing (top-N CM scenarios)
- [ ] Multi-CM hierarchy: TM-CM clearing for sub-TMs, obligation aggregation
- [ ] T+0 parallel settlement path for equity cash
- [ ] IRD specifics: bond futures delivery basket, conversion factors, CTD
- [ ] Enhanced dashboard: multi-segment views, margin utilization, SGF status
- [ ] Integration tests: end-to-end across all segments

---

## References

- [NSE Clearing — Settlement Cycle](https://www.nseclearing.in/clearing-settlement/capital-market/settlement-cycle)
- [NSE Clearing — Securities Settlement](https://www.nseclearing.in/clearing-settlement/capital-market/securities-settlement)
- [NSE Clearing — Equity Derivatives Settlement Mechanism](https://www.nseclearing.in/clearing-settlement/equity-derivatives/settlement-mechanism)
- [NSE Clearing — Currency Derivatives Settlement](https://www.nseclearing.in/clearing-settlement/currency-derivatives/settlement-mechanism)
- [NSE Clearing — Interest Rate Derivatives](https://www.nseclearing.in/interest-rate-derivatives)
- [NSE Clearing — Corporate Bond Settlement](https://www.nseclearing.in/clearing-settlement/corporate-bond)
- [NSE Clearing — Debt Segment](https://www.nseclearing.in/clearing-settlement/debt-segment)
- [NSE Clearing — Margins (SPAN)](https://www.nseclearing.in/risk-management/equity-derivatives/margins)
- [NSE Clearing — Core SGF & Default Waterfall](https://www.nseclearing.in/core-sgf-default-waterfall)
- [NSE Clearing — Fees, Deposits & Networth Requirements](https://www.nseclearing.in/membership/fees-deposits--networth-requirements)
- [NSE India — Equity Market Clearing & Settlement](https://www.nseindia.com/static/products-services/equity-market-clearing-settlement)
- [NSE India — Equity Derivatives Clearing & Settlement](https://www.nseindia.com/static/products-services/equity-derivatives-clearing-settlement)
- [NSE India — Shortages Handling](https://www.nseindia.com/products-services/equity-market-shortages-handling)
- [NSE India — T+0 Settlement Cycle](https://www.nseindia.com/static/products-services/t0-settlement-cycle)
- [CCIL — Clearing & Settlement Procedure](https://www.ccilindia.com/web/ccil/clearing-and-settlement-procedure5)
- [CCIL — Securities Settlement FAQ](https://www.ccilindia.com/web/ccil/faqmodulesecurity-settlement)
- [Zerodha Varsity — Clearing and Settlement Process](https://zerodha.com/varsity/chapter/clearing-and-settlement-process/)
- [Zerodha Varsity — Physical Settlement in F&O](https://zerodha.com/varsity/chapter/quick-note-on-physical-settlement-2/)
- [NSE Clearing FAQ — Risk Management Margins (PDF)](https://www.nseclearing.in/sites/default/files/2025-01/NCL%20-%20FAQ%20RISK%20MANAGEMENT.pdf)
- [SEBI — Core SGF Composition & Contribution Policy (PDF)](https://www.nseclearing.in/sites/default/files/disclosure-doc/2024-11/Policy%20on%20Composition%20and%20Contribution%20to%20Core-%20SGF_0.pdf)
- [IEPF — NSE Debt Markets Chapter (PDF)](https://www.iepf.gov.in/IEPF/pdf/Chapter_6_2_NSE.pdf)
