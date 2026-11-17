# Implementation status

Last updated 2026-11-17 (after the audit corrections in [AUDIT_2026-11.md](AUDIT_2026-11.md)). Statuses: **DONE** (implemented and tested), **UNVERIFIED** (written,
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
- NOT IMPLEMENTED: incremental update jobs, streaming ingestion, fundamentals, a holiday
  calendar (operator must supply), real-time stale-feed monitoring against a live feed,
  strategies reading news events inside a backtest.
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
- NOT IMPLEMENTED: paper trading of generated code; Upstox and Angel One adapters; live feed; broker instrument sync; live position
  reconciliation against a broker.
- UNVERIFIED: Zerodha order adapter, WhatsApp notifier.

### Platform
- DONE: FastAPI backend, token auth, CLI, Docker images, compose with HTTPS proxy, CI workflow.
- Docker: both images build; the API container was smoke-tested (auth, sandbox). NOT VERIFIED: the web
  container, the compose stack with Caddy/TLS, and CI on GitHub.
- Auth is mandatory (operator token, optional read-only viewer token). Shared tokens, not user accounts.
- NOT IMPLEMENTED: PostgreSQL / TimescaleDB / Redis / MLflow, OpenTelemetry / Prometheus /
  Grafana, multi-user accounts and roles.

## Next highest-priority tasks

1. Import real NSE data (your bhavcopies or Kite credentials), run the quality report, and
   re-pin acceptance test 1 against it. Everything downstream is only as real as this.
2. Verify the unverified charge-schedule entries against primary sources.
3. Build the docker images in CI and run the compose stack end to end.
4. Run a first real `kite_daily` paper week and verify it against Kite; then an intraday/tick feed.
5. Supply a holiday calendar; wire it into quality checks and session awareness.
