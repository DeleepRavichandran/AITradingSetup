# VEGA brain: gap analysis, what was built, and what is still missing (2026-11-25)

The aim: a better brain, not more strategies. It should discover whether the current methodology works, when it works, when it must stop, and whether a proposed improvement is real or noise.

This document has three parts:
1. The gap analysis against the "Master Intelligence Upgrade" specification.
2. The milestones delivered in this round.
3. What remains, stated honestly.

## Branch review

`origin/main`, `claude/practical-maxwell-un2bhi` and `feature/challenger-trader` are all ancestors of `feature/core-correctness`. There is no divergent work to integrate. All work stays on `feature/core-correctness`. Nothing is merged into main.

## 1. Gap analysis

Status key:
- **VERIFIED**: implemented, and a test proves it.
- **UNVERIFIED**: implemented, without a test.
- **PARTIAL**: partly implemented.
- **MISSING**: not built.
- **BLOCKED**: needs an external dependency.

### P0: integrity, correctness and survival

| Requirement | Status | Where |
|---|---|---|
| No fill before the decision; causal fills | VERIFIED | `scheduler.py` (earliest fill, catch-up); `test_core_defects.py` D4 |
| Cash affordability and charges to the paisa | VERIFIED | `execution.py`; tests D3 |
| Partial fills, spread, impact, liquidity | VERIFIED | `execution.py`; `test_execution_portfolio.py` |
| Deterministic recovery after a crash or restart | VERIFIED | `test_minute_screening.py`, D4 restart test |
| Kill switch reduce-only, human reset only | VERIFIED | `risk/limits.py`; D1 tests |
| Operator-only risk changes; viewer key cannot write | VERIFIED | API auth; `test_trade_limit.py`, `test_integrity.py` |
| **Corrupt input quarantined** | VERIFIED (new) | `store.bar_defect` and the `event_quarantine` table. Before this round, impossible bars (NaN, price ≤ 0, high below close) went straight into the ledger the bot trades on. |
| Stale and incomplete data detected | VERIFIED | Catch-up rule; coverage hold; brain gates (`test_vega_brain` 02/03) |
| **Evaluation criteria protected from the evaluated system** | VERIFIED (new) | `integrity.criteria_manifest`: hash in the ledger; a change blocks promotion until the operator approves that exact hash |
| **Financial history protected** (no hidden or edited losing trades) | VERIFIED (new) | `integrity.record_digests` seals each closed day's fills; suspension rule S5 |
| **Cash reconciles with every fill** | VERIFIED (new) | `integrity.cash_reconciliation`, exact to the paisa on the test ledger |
| Dashboard figures reconcile with the ledger | PARTIAL | Paper Portfolio shows equity = cash + reserved + holdings. The /today figures and ledger figures come from the same engine, but are not cross-checked by an independent test for every page. |
| Instrument-specific settlement (T+1, delivery) | PARTIAL | Charges per product (MIS/CNC). Settlement timing is not modelled. |
| Limitations of candle-based execution reported | VERIFIED | Fills at the next bar; stated on the pages and in `docs/CONTEST.md` |

### P1: higher intelligence

| Requirement | Status | Where |
|---|---|---|
| Pre-registered hypotheses with falsification criteria | VERIFIED | `integrity.py` (7 hypotheses, including H-ABSTAIN) |
| Immutable experiment records (pass and fail) | VERIFIED | Hash-chained ledger; `test_08` |
| Multiple-testing correction | VERIFIED | Benjamini-Hochberg (hypotheses, configurations). `research/validation.py` and `brain/critic.py` have deflated Sharpe and PBO. |
| Placebo and randomised controls (with power) | VERIFIED | `integrity.placebo`; negative and positive controls in `test_10` |
| Cost and delay stress | VERIFIED | `integrity.stress` |
| Regime sensitivity | VERIFIED | Journal regime split |
| Drift and strategy decay | VERIFIED | Page-Hinkley, calibrated by simulation; per strategy type in `brain.decay` |
| **Uncertainty gates** (fresh, complete, edge, familiar, contradictions) | VERIFIED, observation only (new) | `brain.gate_table` (point in time). Out-of-distribution test against earlier sessions at the same minute. |
| **Abstention** when evidence is insufficient | VERIFIED | Integrity verdict "abstain"; unknown gates never count as a pass |
| **Decision vs outcome vs execution vs learning value** | VERIFIED (new) | `brain.trade_quality` |
| **Knowledge with statuses, versions, corrections** | VERIFIED (new) | `brain.Knowledge` on the evidence ledger. External text is refused. |
| **Counterfactual shadow outcomes** | VERIFIED (new) | `brain.shadow`, with alternatives fixed in advance, stored separately |
| Rejected and missed opportunities without hindsight | VERIFIED | `opportunities.py` plus isolation tests |
| Correlated strategies and shared risk | PARTIAL (new) | `brain.shared_risk` (concurrency, correlation by type after 5 sessions). Not yet used to size positions. |
| Calibration | PARTIAL | Rank correlation of score with outcome. Reliability diagrams and Brier scores are only meaningful for probabilities, and the allocator emits a score. The allocator's internal Brier score exists in its checkpoint. |
| Bias defence | PARTIAL | 14 biases have a guard and a check (see the Evidence Integrity page). The cognitive biases in the spec (narrative, herding, sunk cost, …) are mostly addressed by design: no human-style discretion, fixed rules and pre-registration. They have no separate detector. |
| Memory-poisoning and prompt-injection defence | VERIFIED | Knowledge accepts computed sources only; trading modules never read news text; the command parser refuses orders and resets |

### P2: methodology discovery

| Requirement | Status | Where |
|---|---|---|
| Champion vs challenger, frozen versions, isolated portfolios | VERIFIED | `challenger.py`, `contest.py` (frozen manifest, coverage rule) |
| Promotion gates plus operator approval | VERIFIED | `integrity.promotion`; `POST /api/integrity/decision` |
| Generating new methodologies | PARTIAL | The research loop, Strategy Factory and Learning Brain generate candidates on daily data; nothing new is validated on real intraday data yet |
| Swing, positional, long-term, F&O, ETF, SIP decision books | MISSING / BLOCKED | Daily history exists on the laptop (swing is buildable). F&O needs licensed chain and margin data. |

### P3: experience

| Requirement | Status |
|---|---|
| Mission Control, Opportunity Radar with dossiers, Paper Portfolio, Evidence Integrity | VERIFIED (e2e) |
| Brain Intelligence (gates, knowledge, corrections, shadow, decay, measures, not-built list) | VERIFIED (e2e) |
| Learning Laboratory | PARTIAL: split across Incumbent vs Challenger, AI Research Brain, Experiment Memory and Brain Intelligence. There is no separate page, to avoid a duplicate research surface. |

## 2. The 30 mandatory tests

All 30 are in `tests/screening/test_vega_brain.py`, or mapped there (`MAPPED`, itself tested) to an existing test.

| # | Test | # | Test |
|---|---|---|---|
| 1 | `test_01_rejects_future_information` | 16 | D4 late session creates no orders |
| 2, 3 | `test_02_03_detects_stale_and_incomplete_data` | 17 | `test_statistics` (Page-Hinkley) |
| 4, 24 | `test_04_24_contradictory_evidence…` | 18, 19 | `test_18_19_knowledge_survives_versions…` |
| 5, 30 | `test_05_30_abstains…` | 20 | D1 kill switch keeps protective exits |
| 6 | integrity cannot change rules or the live flag | 21 | `test_21_unfamiliar_market_conditions…` |
| 7 | loosened criteria detected | 22 | `test_22_more_trading_is_not_rewarded` |
| 8 | `test_08_failed_experiments_are_preserved` | 23 | only post-registration data counts |
| 9, 10 | `test_09_10_overfit…`, `test_10_placebo_has_power…` | 25 | `test_25_missing_functionality_is_reported` |
| 11 | `test_11_one_trade_is_never_a_finding` | 26 | hidden losing trade detected |
| 12 | `test_12_shadow_trades_are_separate…` | 27 | `test_27_shared_risk…` |
| 13 | corrupt bars quarantined | 28 | `test_28_capital_limits_are_pinned…` |
| 14 | `test_14_malicious_external_instructions…` | 29 | `test_29_no_model_needed_and_no_network` |
| 15 | D4 restart reproduces the ledger | – | `test_reproducible` |

## 3. Milestone report (this round)

**Files**
- New:
  - `services/screening/brain.py`
  - `tests/screening/test_vega_brain.py`
  - `apps/web/app/brain/page.tsx`
  - `docs/VEGA_BRAIN.md`
- Extended:
  - `services/screening/integrity.py` (criteria, digests, reconciliation, S5, H-ABSTAIN)
  - `services/screening/store.py` (quarantine)
  - `services/screening/daily_review.py` (integrity and brain steps)
  - `services/api/main.py` (`/api/brain`)

**Defects found and fixed**
1. Corrupt bars entered the ledger unchecked.
2. Nothing stopped a later edit of the evaluation thresholds, or deletion of a losing fill, from improving the apparent results. Both are now detected.
3. The decision stream and integrity ledger were scrollable without keyboard access. Fixed after the axe test flagged them.
4. The new font made definition lists overflow on 360 px screens.

**Preserved**
- Every page, API, risk limit and safety control.
- Trading behaviour is unchanged: the brain is observation only, and a source scan proves no trading module imports it.

**Evidence types**
- Every number in this round's tests comes from synthetic replays. They prove behaviour, not market edge.
- The live account's first integrity and brain reports are produced on the laptop after the next close.
- Expected, not yet observed:
  - "abstain"
  - recency and automation flags for rules 1.1–1.4
  - most entries showing no positive expected edge at decision time

**Remaining weaknesses**
- Survivorship: the watchlist is today's stocks.
- The allocator's score is not a calibrated probability.
- Gates are observation only. Enforcing them is a rule change, which needs H-ABSTAIN's confirmatory result and your approval.
- No swing or longer-horizon book yet.
- Dashboard–ledger reconciliation is only partial.

**External blockers**
- F&O data (licensed chains and margins).
- Institutional flow data.
- India VIX history (not stored with the live bars).

**Next priorities**
1. Run H-ABSTAIN and H-BLACKOUT to their confirmatory sample sizes.
2. Swing book on the laptop's daily history, with walk-forward and a sealed holdout.
3. Correlation-aware sizing proposal (as a challenger).
4. An independent dashboard–ledger reconciliation test for every money figure.
