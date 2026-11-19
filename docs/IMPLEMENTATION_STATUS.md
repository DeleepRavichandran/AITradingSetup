# Implementation status

Last updated 2026-11-19 (one-minute screening slice — see [SCREENING.md](SCREENING.md) and [PLATFORM_V5_STATUS.md](PLATFORM_V5_STATUS.md); trading-readiness additions: holidays, corporate actions, market context, scheduled events, paper trading of screened configurations, Windows). Earlier: the audit corrections in [AUDIT_2026-11.md](AUDIT_2026-11.md). Statuses: **DONE** (implemented and tested), **UNVERIFIED** (written,
tested only against mocks — never run against the real service), **BLOCKED** (cannot be done
without something only the operator can supply), **NOT IMPLEMENTED**.

## Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Import an actual authorised NSE dataset | **BLOCKED** | Parsers for legacy and UDiFF bhavcopies exist and pass on layout samples with made-up values. No real exchange file has been imported: none is available here and fetching one unlicensed is out of bounds. Needs your files. |
| 2 | Reproducible equity backtest | DONE | identical `result_hash` across runs; golden hashes pinned |
| 3 | Reconcile every trade against the ledger | DONE | exact integer identities; per-fill ledger check via API |
| 4 | Valid historical charges | DONE for 2023-04 onward; **pre-2023 and stamp-duty entries are unverified** | hand-computed cases; effective-date tests |
| 5 | Simulate a futures contract | DONE on synthetic contracts | daily MTM, rolls, settlement at underlying close |
| 6 | Simulate an option with its own premium data | DONE on synthetic contracts | fills inside the contract's own bar; intrinsic settlement |
| 7 | Compare strategies on the same assumptions | DONE | comparison flags mismatched data, period or costs |
| 8 | Generate a new strategy through an AI agent | DONE (grammar inventor); LLM inventor **UNVERIFIED** | generated code stored with hash |
| 9 | Safely test AI-generated code | DONE | static allow-list + isolated subprocess |
| 10 | Independently reject a flawed or leaky strategy | DONE | look-ahead trap catches a cheating strategy; 11 flawed-evidence cases |
| 11 | Auditable walk-forward report | DONE | folds link to stored runs and dataset hash |
| 12 | Results in a functional dashboard | DONE | 18 pages, browser-tested |
| 13 | Stream authorised real-time data | **BLOCKED / NOT IMPLEMENTED** | no live feed adapter; needs broker credentials and a data entitlement |
| 14 | Paper orders and position reconciliation | DONE on replayed bars | paper session reproduces the backtest fill for fill |
| 15 | Stop safely on data, risk or connection failure | DONE | stale feed, feed error, loss limit, kill switch |
| 16 | AI-generated code cannot enable real-money trading | DONE | four independent barriers, each tested |

## By area

### Data
- DONE: immutable content-hashed datasets, raw-file retention, ingest audit trail, quality report,
  point-in-time portal, corporate actions (splits, bonuses, cash dividends), index membership
  records, generic CSV import, synthetic fixture, event store, market-structure timeline.
- UNVERIFIED (mock-tested): Kite login (`nseql kite-login`), instrument master, chunked historical candles and `nseql import-kite` for equities and indices. Limits: current listings only (no delisted names), no expired F&O contracts, no corporate-action feed, no point-in-time index membership, price bands unknown.
- DONE: NSE holiday list loader (`nseql holidays`, CSV copied from the exchange circular; years it is
  complete for are recorded). The scheduler skips closed days once caught up (no login nag on a holiday),
  warns once if the list does not cover the current year, and the daily feed waits until every symbol's
  candle exists before processing today (from the next day it delivers what exists and records the gap).
  The operator must still supply the list.
- DONE: split/bonus detection (`nseql corporate-actions`): overnight drops that match a common ratio are
  reported (also in the quality report and after `import-kite`); confirmed actions are attached from a CSV
  as a NEW dataset version. An action is refused if the prices do not drop on that date (protects data
  that is already adjusted from being adjusted twice). Kite gives no corporate-action feed.
- DONE (mock-tested against Kite): market context dataset (`nseql import-context`): India VIX, NIFTY 50
  and 8 sector indices, USDINR futures, MCX crude and gold (continuous futures). Currency and commodity
  values are used from the next trading day because they close after the stock market. UNVERIFIED against
  the real API; currency and MCX series need those segments on the Kite account.
- DONE: scheduled events with a `known_from` date (`nseql events`): results dates, RBI policy, Union
  Budget, US Fed decisions from an operator CSV; monthly F&O expiry generated from the rule (switch to
  Tuesday dated 2025-09-01, unverified). Ships no event data.
- NOT IMPLEMENTED: incremental update jobs, streaming ingestion, fundamentals, real-time stale-feed
  monitoring against a live feed, news text inside a backtest, global markets (S&P 500, US yields: no
  source chosen), a forward news digest.
- Known gaps: stock-option physical settlement is approximated by cash settlement (flagged);
  dividends are credited on ex-date; index membership is stored but strategies trade the listed
  universe, not a point-in-time index basket.

### Backtester
- DONE: market / limit / stop / stop-limit, OCO, atomic multi-leg, partial fills, rejections,
  cancellations, spread, impact, participation cap, tick and lot sizes, price bands, position and
  order limits, cash and margin, short-selling rules, MIS square-off, overnight carry, futures and
  option lifecycle, FIFO P&L, full ledger, dated charges, deterministic replay.
- Approximations: margin is not exchange SPAN and has NOT been reconciled against exchange figures (use
  `scripts/reconcile_margin.py` with your own); forced exits trade from the next bar with normal liquidity limits; final settlement uses the underlying's close rather
  than the exchange's last-half-hour average; no bid/ask data, spread is a parameter.
- NOT IMPLEMENTED: tick or sub-minute simulation, currency / commodity / interest-rate
  derivatives (instrument kinds exist, nothing else), stock-lending, market-wide circuit halts.

### Strategies
- DONE: `screened_config` runs any configuration from the screening universe as a normal strategy (backtest
  and paper), recomputing the screener's weights each day from history seen so far; fills are identical
  to the screener's precomputed weights (tested on 3 families).
- DONE (library, 19): SMA cross, multi-timeframe trend, momentum rank, Donchian breakout, volume
  breakout, previous-day breakout, RSI / Bollinger / z-score reversion, opening-range breakout,
  VWAP reversion, gap fade, futures trend with rolls, futures calendar carry, twelve option
  structures (long call/put, verticals, credit spreads, straddles, strangles, iron condor,
  butterfly), calendar spread, logistic-regression direction model with purged retraining,
  buy-and-hold and random-entry baselines.
- NOT IMPLEMENTED: reinforcement learning (intentionally — non-RL baselines come first),
  volatility forecasting models, short equity strategies on daily bars (blocked by delivery rules).
- The generated-strategy grammar covers long-only daily cash-equity rules only.

### Research
- DONE: everything in ARCHITECTURE §6.
- UNVERIFIED: Claude-backed inventor and analyst (no credentials available here).
- Limits: the grammar has 8 templates and 4 structural mutations, so without an LLM a dataset is
  exhausted after a handful of runs; the learning agent is a bandit plus failure-to-next-step
  rules, not a learned model.

### Portfolio, paper, execution
- DONE: shared-ledger multi-strategy simulation; equal / risk-parity / min-variance /
  vol-target weights estimated on development data; correlation, overlap, sector and
  delta-equivalent exposure; paper sessions; kill switch; router controls; live gate.
- DONE: paper sessions persist across restarts (journal + deterministic replay); the kill switch state
  persists; a scheduler advances sessions and sends an end-of-day summary.
- UNVERIFIED (mock-tested): end-of-day paper trading from Kite daily candles (`kite_daily` feed). Daily
  cash-equity library strategies only; needs a Kite login each day; no intraday or tick feed.
- DONE: paper trading of screened configurations (`screened_config`), including from the end-of-day Kite feed.
  Context-filtered configurations are refused for paper trading (no live context/event feed).
- NOT IMPLEMENTED: paper trading of generated code; Upstox and Angel One adapters; live feed; broker instrument sync; live position
  reconciliation against a broker.
- UNVERIFIED: Zerodha order adapter, WhatsApp notifier.

### Platform
- DONE: FastAPI backend, token auth, CLI, Docker images, compose with HTTPS proxy, CI workflow.
- Docker: both images rebuilt on `35fd653`; API and web containers smoke-tested (see TEST_REPORT). CI on GitHub
  passes. The compose stack (API + web) ran end to end on 2026-11-19. NOT VERIFIED: the Caddy/TLS profile. Fixed: compose did not read `.env` from the
  repository root; the documented commands now pass `--env-file .env`.
- Windows laptop: `scripts/windows/` (setup, start, command wrapper) and docs/WINDOWS.md; `tzdata` added for
  Windows; the sandbox reports itself unavailable off Linux (fails closed). NOT RUN on Windows; the
  PowerShell scripts were not syntax-checked.
- Auth is mandatory (operator token, optional read-only viewer token). Shared tokens, not user accounts.
- NOT IMPLEMENTED: PostgreSQL / TimescaleDB / Redis / MLflow, OpenTelemetry / Prometheus /
  Grafana, multi-user accounts and roles.

### One-minute screening (see SCREENING.md)
- DONE (on a replayed feed): central minute scheduler, shared incremental features (1m/5m/15m/1d),
  intraday and swing configurations up to 50,000 with deterministic ids, decisions, forward paper ledger,
  idempotent processing, checkpoint and crash recovery, gap/late/conflict handling, API and dashboard page.
  Streaming decisions equal an independent batch reference. Benchmarked at 1k/5k/10k/50k (synthetic
  stream): p99 cycle 211 ms at 50k, no missed evaluations, ledger identical after SIGKILL + restart.
- NOT IMPLEMENTED: a live 1-minute feed (needs a licensed real-time entitlement); positional, long-term,
  F&O, ETF, mutual-fund and SIP engines; shared capital and volume caps in the paper loop.

### Learning Brain (see LEARNING_BRAIN.md)
- DONE: episodic memory (append-only, hash-chained, point-in-time), semantic findings with FDR control,
  procedural screening (Thompson sampling), meta-learning (calibrated model + approach bandit) with
  self-validation gates, point-in-time regime eligibility (research only), self-critic (FDR, cost stress,
  de-duplication, PBO), policy versions with frozen benchmark, forward shadow evaluation, human-only
  promotion and automatic rollback, learning dashboard, CLI and API.
- DONE: screening tier for 49,728 configurations (process pool, shared memory-mapped indicators,
  budgets); agreement with the event engine measured.
- RESULT: memory-informed research has NOT shown a reliable out-of-sample improvement on the synthetic
  fixture (sealed window, Holm-adjusted). Negative controls show no false positives.
- DONE: market-context extension of the universe (+33,152 configurations = 82,880 with context): 5 context
  states and 3 scheduled-event states as filters. `nseql context-test` compares each filter with its
  identical unfiltered twin against a time-shifted placebo (development + validation only; Holm-adjusted).
  Checked: no false positives in 30 runs with random context; a deliberately leaked filter is detected.
  NOT RUN on real context data. The Learning Brain's decision loop still uses the base universe only.
- NOT IMPLEMENTED: multi-machine evaluation queue; code-level self-modification.

## Next highest-priority tasks

1. Import real NSE data (your bhavcopies or Kite credentials), run the quality report, and
   re-pin acceptance test 1 against it. Everything downstream is only as real as this — including
   the Learning Brain, which needs several years of real history for a meaningful test.
2. Verify the unverified charge-schedule entries against primary sources.
3. Build the docker images in CI and run the compose stack end to end.
4. Run a first real `kite_daily` paper week and verify it against Kite; then an intraday/tick feed.
5. Load the NSE holiday list, scheduled events and the Kite context dataset; run `nseql context-test` on
   real data before trusting any context filter.
