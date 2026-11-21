# Master Prompt V5 — status against the requested platform

Updated 2026-11-20 (realistic execution and one shared portfolio in the minute loop). This compares the multi-asset, multi-horizon platform requested in Master Prompt
V5 with what exists in the repository today. It complements [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

Categories:

- **Verified working**: implemented and exercised by tests or measured runs in this repository.
- **Implemented, unverified**: written, but never run against the real service or data it targets.
- **Mock-tested / simulated**: tested only with synthetic data or mocked services.
- **Incomplete**: part of the capability exists.
- **Blocked**: needs credentials, licensed data or a decision only the operator can make.
- **Not implemented**: nothing exists yet.

A capability is listed as verified only if a test or a recorded run demonstrates it. Any 50,000-configuration
figure below comes from a *synthetic* minute stream; no real-time market feed is connected.

## Baseline before this milestone

- 294 tests passed as root; 291 passed and 3 skipped as an unprivileged user (2026-11-19).
- GitHub CI green on `e5112e4`.
- Historical screener: 49,728 configurations in 8.7 s on 4 workers (vectorised daily tier).

## What this milestone added: the one-minute screening slice (`nseql.screening`)

| Capability | Status | Evidence |
|---|---|---|
| Central scheduler, a cycle every 60 s while the session is open (thread aligned to minute boundaries + grace) | **Verified working** (on a replayed feed) | `test_background_clock_runs_a_cycle_after_each_minute_boundary`; 3-minute wall-clock run at 50,000 configurations (docs/SCREENING.md) |
| Shared incremental feature pipeline (1m → 5m, 15m, 1d; SMA, prior high/low, RSI), only closed timeframes recomputed | **Verified working** | `test_incremental_features_equal_the_batch_reference` (>3,000 bar/feature comparisons against an independent pandas implementation, including illiquid gaps) |
| Historical-vs-streaming signal parity | **Verified working** | `test_streaming_decisions_equal_an_independent_batch_reference`: ~17,000 BUY/EXIT decisions from the streaming engine equal a per-configuration reference state machine on batch features. It found and fixed a real defect: rule comparisons flipped on floating-point noise when prices were flat. |
| Intraday horizon (1m/5m/15m): entries until 15:00, square-off from 15:15, MIS charges | **Verified working** (synthetic data) | `test_intraday_positions_are_flat_after_the_close_and_swing_positions_carry` |
| Swing horizon (daily rules, stops checked every minute, positions carried overnight, CNC charges) | **Verified working** (synthetic data) | same test; parity test covers stops |
| Up to 50,000 registered configurations with deterministic ids | **Verified working** | `test_registry_ids_are_deterministic_and_reach_50k`; benchmark |
| Idempotency: the same market event twice creates no duplicate evaluation, signal or order | **Verified working** | `test_the_same_event_twice_never_duplicates_anything` (duplicate bars, repeated cycles, an old minute fed to the engine) |
| Conflicting and late events quarantined, not applied | **Verified working** | `test_conflicting_and_late_events_are_quarantined_not_applied` |
| Checkpoint + crash recovery reproduces the uninterrupted run exactly | **Verified working** | `test_crash_and_restart_reproduce_the_uninterrupted_run`; benchmark SIGKILL test at every size |
| Feed gaps recorded as `no_data` minutes (never silently skipped) | **Verified working** | `test_missing_minutes_are_recorded_as_gaps` |
| Kill switch stops simulated orders, screening continues | **Verified working** | `test_kill_switch_stops_simulated_orders_but_not_screening` |
| Forward paper ledger kept separate from research/backtest ledgers; fills at next minute's open; dated charges | **Verified working** (synthetic data) | `test_paper_ledger_is_consistent` |
| Decision vocabulary BUY / SELL / HOLD / ACCUMULATE / REDUCE / EXIT / REBALANCE / NO TRADE | **Incomplete** | All eight are defined. This slice emits BUY, EXIT, HOLD and NO TRADE; the rest need products (shorting, long-term, portfolios) that are not in the slice yet. |
| API (`/api/screening/*`, server-side pagination and filters) and dashboard page "Strategy Monitor" with Intraday / Swing views | **Verified working** (API by test; page by type-check and build) | `tests/screening/test_screening_api.py` |
| Real-time 1-minute feed | **Implemented, unverified**: Angel One SmartAPI polling of completed 1-minute candles (`nseql.screening.feeds`), with automatic TOTP login, warm-up and unattended start in the API. Mock-tested only; not yet run against the real service. KiteTicker is not implemented. | `tests/unit/test_angelone.py` |

## Requested scope by area

| Area (V5 section) | Status |
|---|---|
| §2 Audit and test baseline | Verified working (this document, TEST_REPORT.md) |
| §3 NSE equities, indices | Verified working on synthetic data; **real data blocked** (no licensed import has been run) |
| §3 ETFs, REITs/InvITs, debt, G-Secs, T-bills | **Not implemented** (an instrument kind exists for ETF only) |
| §3 Futures and options | Backtester supports them on **synthetic contracts only**; real contract history **blocked**; option chains, IV, Greeks, OI analytics **not implemented** |
| §3/§13 Mutual funds (AMFI NAV), SIP/SWP/STP, XIRR, goals | **Not implemented** |
| §4 One-minute scheduler, intraday + swing engines | Verified working (on a replayed feed) — see above |
| §4 Positional (weekly/monthly) and long-term engines | **Not implemented** |
| §4 Expiry and special sessions | **Incomplete**: holidays are supported (operator list); expiry dates are generated; special and Muhurat sessions are not modelled |
| §5 Tier A fast research engine (49,728 daily configurations) | Verified working (existing) |
| §5 Tier B event-driven simulator | Verified working (existing) |
| §5 Incremental live evaluation at 50k | Verified working on a synthetic stream (benchmark) |
| §5 Stateful parallel or distributed workers, backpressure | **Not implemented**: one process evaluates every configuration. At 50k, p99 cycle time is far below the 60 s budget, so distribution is not justified yet. |
| §6–8 Research agents, memory, controlled self-improvement | Existing. **Learning is not demonstrated** (synthetic sealed test; see LEARNING_BRAIN.md). No new learning claim is made. |
| §9 Realistic execution | Existing event engine. The minute loop now also has volume-capped partial fills, spread and impact, per-order brokerage caps and entry time-outs: **verified working** on synthetic data (test_execution_portfolio.py). |
| §10 Portfolio Brain | **Incomplete.** The minute loop has one shared capital pot with cash reservations, per-symbol and gross exposure caps, a position cap and a daily loss limit: **verified working** on synthetic data. Not built: signal ranking for scarce capital, strategy-correlation limits, tail-risk measures, tax lots across products, separate goal portfolios. |
| §11 Options intelligence | **Not implemented** (real option premium data is blocked) |
| §12 Fundamentals and events | **Incomplete**: scheduled events with known-from dates are implemented. Fundamentals and news are **not implemented**; data is **blocked** by licensing. |
| §15 Real-time paper trading | **Incomplete**: forward paper ledger on replayed minutes; end-of-day Kite paper sessions are **unverified**. Live feed **not implemented**. |
| §16 Data quality and operations | **Incomplete**: gaps, late and conflicting events, and cycle latency are recorded. No CPU/memory pressure monitors and no alerting. |
| §17 Security: generated code in separately isolated workers; no privileged API container | **Not implemented**. The API container still needs `CAP_SYS_ADMIN` to build sandbox namespaces. This is the highest-risk open security item. |
| §18 Dashboard (26 modules) | **Incomplete**: 20 pages exist, including the new Strategy Monitor. Futures, Options, ETF, Mutual Fund, SIP, Long-Term and Operations pages do not exist. |
| §19 PostgreSQL / ClickHouse / Redis Streams / Prometheus / Ray | **Not implemented**. SQLite (WAL) and NumPy meet the measured load of this slice. |
| Live order routing | Disabled. Unchanged. |

## Highest-risk open items

1. **No real data** has been imported, so every strategy result in this repository is on synthetic data.
2. **Privileged API container**: the strategy sandbox needs `CAP_SYS_ADMIN`. Generated code should move to a separate, isolated worker service.
3. **No real-time feed**: the minute loop is proven on a replayed stream only.
4. **Capital allocation is first-come**: when signals compete for scarce capital the portfolio serves them in registry order; deciding which signal deserves the money is not built.
