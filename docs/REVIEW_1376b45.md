# Review of 1376b45: trading core, evidence, challenger, learning, scale (2026-11-25)

Branch `feature/core-correctness`, created from `feature/challenger-trader` at 1376b45. Real-money routing stays
disabled throughout (`LIVE_TRADING_ENABLED=false`). Every order in this work is simulated. Market data in this
container is **synthetic**: no licensed history and no broker access exist here (see "Real data" below).

## 1. Trading core: before / after

The same tests ran against the reviewed commit (an isolated worktree, imports pointed at it) and against this branch.
The raw "before" result is in `docs/experiments/2026-11-25/before_1376b45_tests.txt`.

| Defect (reproduced through `MinuteScheduler`) | Test | 1376b45 | This branch |
|---|---|---|---|
| D1 pending buys filled after the kill switch | `test_d1_kill_switch_blocks_pending_buys_before_the_next_bar` | FAIL (filled at the next bar) | pass |
| D1 rest of a partial buy filled after the halt; protective exit kept | `test_d1_..._partial_buy_and_keeps_protective_exits` | FAIL | pass |
| D1 incumbent engine: halt with working orders | `test_d1_..._real_incumbent_engine` | FAIL (cycle crashed: `KeyError 'qty'`) | pass |
| D1 challenger engine: breakout buys after the halt | `test_d1_..._real_challenger_engine` | FAIL (filled) | pass |
| D2 two simultaneous 8 x Rs 100 buys vs a Rs 1,000 cap | `test_d2_simultaneous_buys_respect_the_symbol_cap` | pass (already handled at submission) | pass |
| D2 20% gap at the fill | `test_d2_a_price_gap_is_rechecked_at_the_fill` | FAIL (Rs 1,080.90 > Rs 1,000) | pass (cut to the cap) |
| D2 partial fill, cancel, retry | `test_d2_partial_fill_then_retry_counts_what_is_already_held` | pass | pass |
| D3 Rs 10,000 delivery buy in a Rs 10,000 account | `test_d3_delivery_buy_never_takes_cash_below_zero_after_charges` | FAIL (cash -Rs 20.78) | pass (cash >= 0, paisa-exact identity) |
| D3 reservation covers the dated charges | `test_d3_reservation_covers_dated_charges_to_the_paisa` | FAIL | pass |
| D4 whole session delivered at 15:31 | `test_d4_whole_session_delivered_late_creates_no_live_orders` | FAIL (885 "live" fills) | pass (0 orders; 370+ catch-up minutes) |
| D4 normal latency, timeline recorded | `test_d4_normal_latency_trades_and_records_the_timeline` | FAIL (no timeline columns) | pass |
| D4 30-minute outage | `test_d4_outage_minutes_are_catch_up_and_trading_resumes_after` | FAIL | pass (29 catch-up minutes, then trading resumes) |
| D4 restart parity | `test_d4_restart_reproduces_the_uninterrupted_ledger` | pass | pass |
| D4 partly stale watchlist | `test_d4_partly_stale_watchlist_trades_only_fresh_symbols` | pass | pass |
| E2 standalone liquidity is order independent | `test_e2_standalone_fills_do_not_depend_on_unrelated_configurations` | FAIL (100 alone, 0 with an unrelated config) | pass |
| E2 a shared portfolio still shares the bar | `test_e2_a_shared_portfolio_still_shares_the_bar` | pass | pass |
| E3 one 09:16 candle is not a completed session | `test_e3_one_candle_is_not_a_completed_session...` | FAIL (no coverage rule) | pass |
| E3 forward manifest frozen at inception | `test_e3_forward_manifest_is_frozen...` | FAIL | pass |
| E4 execution versions recorded; old ledgers flagged | `test_e4_execution_version_is_stamped...` | FAIL | pass |

The reviewed D2 case (two 8-share buys at Rs 100 against a Rs 1,000 cap) could not be reproduced at submission:
the second buy is deferred there. What did break is the cap after a price gap at the fill, which is fixed.

E1 (the Daily Review reconstructed 188 trades for -Rs 33.16 while the ledger closed 15 for -Rs 11.39) is fixed at the
root rather than by reconciling two reconstructions. The loss study now reads the account's own ledger and its
live trade journal, with no replay. Every round trip is reconciled by order identity, quantity, exit price and net
P&L; an unreconciled study publishes no headline (`test_loss_study`, `test_daily_review`).

Semantics are documented in `docs/CONTEST.md` section 8.

## 2. Tests

- **Python:** 477 collected, 477 passed, 0 failed, 0 skipped (full suite on the final code of this branch, 17 min 45 s).
- **Web:** TypeScript check clean, production build clean, end-to-end 69 of 69 passed, 0 failed (Playwright, mocked API).
- **Blocked (not runnable here):**
  - Angel One live verification (`angel-verify`): no broker credentials in this container.
  - A real-data run of the experiment: no licensed 1-minute history.

## 3. Experiment (sections 3 and 4): SYNTHETIC data

Protocol (`services/screening/experiment.py`):
- Phases in order: development 6 days, walk-forward validation 3 x 5 days, holdout 15 days. Rs 1 lakh, rules 1.2,
  20 symbols, 6,120 configurations, realistic fills and dated charges.
- The manifest (parameters, selection, primary metric) is frozen before the holdout is generated.
- Every variant tried is in `variants.jsonl`.
- Data: `factor_stream` (20% of symbols carry a small planted upward drift). The learning null control is
  `minute_stream` (random walk).
- Evidence: `docs/experiments/2026-11-25/`.

### Hypotheses (from the incumbent's 23 Nov live day and journal)

| | Mechanism | Falsified if | Isolated change | Validation (vs INC-L) | Decision |
|---|---|---|---|---|---|
| H1 | Turnover consumes the gross edge | gross P&L before costs is not positive; or holding cash does not help | fund only qualified configurations | 0 trades (+0.24%/day, [-0.58%, +0.96%]), 2/3 windows | accepted by the rule, but **degenerate** (see below) |
| H1b | Same, but keep exploring | as H1 | quarter size until qualified | 0/3 windows | rejected |
| H2 | Opening-range entries lose | first-30-minute trades are not worse; blackout does not help | no entries in the first 30 minutes | +0.06%/day [-0.06%, +0.17%], 2/3 | accepted |
| H3 | Thin-liquidity fills pay impact | impact is small; a lower cap does not help | participation 10% to 2% | +0.11%/day [-0.08%, +0.33%], 2/3 | accepted |

On validation, the incumbent's gross P&L was +Rs 1,082 and its costs were Rs 4,515 (spread and impact Rs 2,277, charges
Rs 2,239). That supports H1's mechanism: the costs were about four times the gross edge.

**Iteration 1 (predeclared): the final challenger was H1+H2+H3 and it never traded.** H1 as specified can never
qualify a configuration, because evidence only accrues from trades that were funded. It is cash by construction. The
predeclared rule accepted it: cash beat a losing incumbent on 2 of 3 validation windows. The weakness is in the
rule, which needs a lower interval bound above zero.

Holdout, 3-23 Feb:

| Account | Net | Max drawdown | Trades |
|---|---|---|---|
| INC-L | +Rs 1,834 | -6.4% | 44 |
| INC-F | +Rs 1,098 | | |
| CH-0 | -Rs 4,146 | | |
| Final | Rs 0 | | 0 trades |

INC-L's best day was +Rs 2,452, 134% of its net.

**Iteration 2 (a recorded follow-up).**
- H1b replaced H1, and was rejected. H2 and H3 keep their iteration-1 validation decisions.
- New untouched holdout: 24 Feb - 16 Mar, after the first one. The generators are sequential, so the earlier days are identical.
- Caution: this is a second look. Two iterations were tried, and I had seen iteration 1's holdout before designing H1b.

| Holdout 24 Feb - 16 Mar (synthetic) | Net | Return | Max DD | Trades | Turnover | Exposure | Gross at bar opens | Spread+impact | Charges |
|---|---|---|---|---|---|---|---|---|---|
| **Challenger INC-L + H2 + H3** | **+Rs 3,396** | +3.83% | -5.8% | 122 | 19.9x | 77% | +5,884 | -1,137 | -1,351 |
| Incumbent INC-L (learned) | +Rs 777 | +0.86% | -6.1% | 84 | 14.9x | 74% | +2,663 | -875 | -1,012 |
| Incumbent INC-F (fixed selector) | -Rs 3,511 | -4.10% | -7.6% | 277 | 49.4x | 74% | +2,154 | -3,109 | -2,556 |
| ORB challenger CH-0 | -Rs 1,483 | -1.65% | -3.9% | 124 | 21.6x | 35% | +919 | -1,370 | -1,031 |
| Exposure-matched baseline (77%, buy the watchlist 09:17, sell 15:16) | -Rs 2,815 | -3.11% | -5.8% | 270 legs | | 77% | -1,033 | -969 | -813 |
| Cash | Rs 0 | 0 | 0 | 0 | | 0 | | | |

Paired daily difference (challenger minus comparator, 95% circular block bootstrap, 15 days):

| Comparator | Mean per day | 95% interval |
|---|---|---|
| INC-L | +0.19% | [+0.03%, +0.33%] |
| CH-0 | +0.37% | [-0.26%, +0.88%] |
| Baseline | +0.47% | [+0.15%, +0.73%] |
| Cash | +0.26% | [-0.52%, +0.90%] |

Stress tests of the challenger on the same holdout:

| Stress | Net | Trades |
|---|---|---|
| Slippage doubled (spread 10 bps, impact 50) | **-Rs 2,304** | 144 |
| One-bar execution delay | +Rs 1,046 | 84 |

Concentration: the best day was +Rs 2,601, 77% of the net. The best symbol, SYN009, contributed +Rs 1,530 of realised P&L.

### Learning: learned selector vs fixed selector

Same candidate pool, dates, capital and execution.

| Data | Learned | Fixed | Learned minus fixed per day |
|---|---|---|---|
| Planted edge, iteration 2 holdout | +Rs 777 (84 trades) | -Rs 3,511 (277 trades) | **+0.34% [+0.22%, +0.45%]** |
| Planted edge, iteration 1 holdout | +Rs 1,834 | +Rs 1,098 | +0.05% [-0.16%, +0.25%] |
| Random walk (null), iteration 2 | -Rs 2,335 | -Rs 5,517 | +0.28% [-0.33%, +0.88%] |
| Random walk (null), iteration 1 | +Rs 294 | +Rs 1,568 | -0.11% [-0.29%, +0.08%] |

Most of the learned selector's advantage is that it **trades less**: its evidence gates refuse proven losers. It is not
mainly better entries: the gross P&L is similar. On the random walk it does not create an edge; it loses less by
trading less.

## 4. Scale (section 5)

See `docs/benchmarks/screening_x20_2026-11-25.json`. Numbers below.

Setup: 50,000 configurations x 300 symbols x 375 minutes (one session), SYNTHETIC random-walk bars, realistic
execution, shared portfolio. Hardware: Intel Xeon @ 2.10 GHz, 4 logical CPUs, 15.7 GB RAM, Python 3.13, numpy 2.5,
pandas 3.1, Linux (this cloud container). Both rows were run on this machine, one after the other, with nothing else running.

| | 1376b45 (same machine) | This branch |
|---|---|---|
| Eligible evaluations | 5,490,322 | 5,490,322 |
| Missed (eligible not evaluated) | 0 | 0 |
| Cycles over the 60 s budget | 0 | 0 |
| Cycle ms, p50 / p95 / p99 / max (ingest + evaluation + ledger write) | 158.5 / 275.4 / 378.5 / 659.7 | 151.3 / 288.8 / 390.1 / 644.2 |
| Engine ms, p50 / p95 / p99 | 26.4 / 53.9 / 83.1 | 25.7 / 55.2 / 72.6 |
| Peak memory | 235.0 MB | 236.8 MB |
| Paper fills | 934 | 934 |
| Restart parity (SIGKILL at minute 232, restart) | not run | **ledger identical**; checkpoint load 0.039 s; caught up in 0.71 s |
| Wall clock, background thread, 3 minutes | not run | lag after each minute's close 2.01-2.02 s (2 s grace); 0 overruns |

The reviewer's 184 ms p99 was measured on different hardware. On this machine the reviewed commit measures 379 ms,
and this branch 390 ms: no meaningful change. A first run of this branch measured 498 ms p99. The cause was the new
worst-case cash reservation, which computed the dated charges for every BUY candidate (about 2,200 a minute). It is now
computed only for candidates that reach the limit checks, and skipped when the position count alone decides the outcome.

**Engine speed is not feed freshness.** These are synthetic bars delivered instantly. Broker-side symbol coverage and
candle age (how late Angel One's 1-minute candles arrive, and for how many of the watchlist's symbols) were **not
measured** here; they need the laptop (`angel-verify`, and the feed-health panel during market hours). A minute
decided more than 55 s after its close is now catch-up and is never traded, so slow candles cost trades, not correctness.

## 5. Real data and live readiness

- **Missing input:** licensed 1-minute history for the watchlist. The protocol needs at least 51 sessions (6 + 15 + 15
  per iteration). The Angel One adapter can fetch it on the laptop (`import-angelone <id> --interval 1m --start YYYY-MM-DD`; Angel One serves at most 30 days of 1-minute bars per request, the adapter pages through them), but that history cannot be
  fetched here.
- **Resume on real data, one command on the laptop:**

  ```
  .venv\Scripts\python.exe -m nseql.cli experiment --dataset <dataset-id-with-1m-bars>
  ```

  The dataset path was exercised here on `synthetic_v1`: it loaded the stored bars and started phase 1.
- **Forward paper contest:** it starts automatically with live screening on the laptop. On the first start after
  pulling this branch, a **new** challenger account is created (`..._x20`), its manifest is frozen at inception, and
  sessions count only with 90% coverage in both accounts. The earlier challenger account is kept. Promotion needs 60
  completed sessions and 100 closed challenger trades, and stays a human decision.
- **Broker coverage and candle age** are a separate measurement from engine speed. They can only be measured on the
  laptop against Angel One: `.venv\Scripts\python.exe -m nseql.cli angel-verify` and the dashboard's feed-health panel.
  Not measured in this run.

## 6. Scores (out of 10)

| Area | Score | Supported by |
|---|---|---|
| Execution correctness | **8** | 14 reproduced defects now pass through the scheduler. 477/477 Python tests. Restart parity identical at 50k. Remaining: a fill is taken at the next bar's open while the decision can come up to 55 s after that bar opened (quantified by the one-bar-delay stress); the kill switch is tested in the engine, not end-to-end through the API. |
| Backtest credibility | **5** | Chronological phases, frozen manifests, reconciled reviews and order-independent standalone replays. But all of it is synthetic, the holdouts are 15 days, and iteration 2 was a second look. |
| Live-data readiness | **4** | Catch-up and coverage rules exist, but they have not been run against Angel One. Broker coverage and candle age are unmeasured. The NSE holiday list is still not loaded on the laptop. |
| Evidence of profitability | **1** | **Unproven.** The only real result is the live incumbent's -Rs 2,248 on 23 Nov. The synthetic challenger lost money under doubled slippage, and its best day is 77% of its profit. |

## 7. Verdict

**What improved:**
- The trading core no longer fills buys after a halt, overspends cash after charges, breaks the symbol cap on a price
  gap, or trades on data that arrived too late.
- Halting with working orders no longer crashes the incumbent's cycle.
- Reviews describe the account's real trades.
- Contests count only properly covered sessions.

**Did the challenger earn more after costs?** On synthetic data with a planted edge, yes: incumbent + H2 + H3 beat the
learned incumbent on an untouched 15-day holdout. The interval was above zero (+0.19%/day [+0.03%, +0.33%]).

But:
- It cannot be distinguished from cash.
- It turns into a loss when slippage doubles.
- Most of its profit came from one day.

On real markets it is **unproven**.

**What would change this conclusion:**
- the same protocol on at least 51 sessions of licensed 1-minute history showing the challenger above the incumbent
  **and** above cash with intervals clear of zero, still positive under doubled slippage, and not dependent on one day;
- then 60 forward paper sessions and 100 closed trades in the frozen contest.

A result that fails any of these keeps the incumbent, or cash, in place.
