# Incumbent vs challenger: measurement fixes, challenger trader and contest

Paper trading only. Real-money order routing stays disabled (`LIVE_TRADING_ENABLED=false`); nothing here can
place a broker order. Promotion of the challenger is the operator's decision.

## 1. Measurement fixes (review 2026-11-24)

Each finding was rechecked against the code at `2c18a54`, reproduced by a test that failed before the fix, and
fixed. Existing ledgers are never rewritten.

| # | Finding | Still present at 2c18a54? | Regression test | Fix |
|---|---|---|---|---|
| F1 | Buy stop-limit (stop 105, limit 106) filled at the 100 open of a bar that later reached 106 | Yes: the reachability pass marked the order triggered, the allocation pass then priced it as a plain limit at the open | `tests/unit/test_execution.py::test_stop_limit_buy_never_fills_below_its_trigger_across_matching_passes` (+ a test that a stop hit on one bar rests as a limit on later bars) | `match_price` is pure; trigger discovery happens once per bar, after every pass (`engine._match`) |
| F2 | A failed SQLite ledger write advanced the watermark; the retry skipped the minute | Yes: the engine state moved before the (rolled-back) write | `tests/screening/test_measurement_fixes.py::test_failed_ledger_write_then_retry_equals_an_uninterrupted_run` (two failures, then retry; cash, positions, fills and ledger identical to an uninterrupted run) | engine state is snapshotted per minute and restored when the write fails (`scheduler._process`) |
| F3 | A finished replay replaced the dashboard's active live-paper account while its scheduler kept running | Yes | `tests/unit/test_angelone.py::test_unattended_api_start_with_angel_one` (replay during live: active run stays `live:...`) | explicit run identities `live:<account>` / `replay:<folder>`; `/api/screening/runs`; `/api/screening/status?run=`; a replay never displaces a running live account and never rewrites `current.json` while one runs |
| F4 | Catch-up marked received bars as `no_data` | Yes: every session minute between the watermark and the newest bar was recorded as a gap, and the later processed row was ignored | `tests/screening/test_measurement_fixes.py::test_catch_up_batch_marks_only_the_genuine_gap_as_no_data` | gaps exclude minutes that have bars |
| F5 | A second rules migration discarded evidence inherited in the first | Yes: carry-over rebuilt evidence from the previous account's own ledger only | `tests/screening/test_carry.py::test_three_successive_accounts_keep_all_inherited_evidence` (+ accounts migrated before the fix) | inherited evidence is stored (`inherited_evidence.json`) and carried on; older accounts rebuild it from `carried_from` |
| F6 | An overnight swing exit lost its entry time and buy-side charges in the daily report | Yes: orders were filtered by day before pairing | `tests/screening/test_measurement_fixes.py::test_overnight_swing_exit_keeps_entry_time_and_buy_charges` | pair across dates first, then keep round trips that exited that day (`trade_report.round_trips`, used by RCA too) |

### Historical reports affected

`python -m nseql.cli ledger-audit --account <folder>` checks a ledger read-only for each finding's trace and
writes `LIMITATIONS.json` beside it (the ledger itself is not modified; tested byte-for-byte).

| Report | Affected by | Limitation |
|---|---|---|
| Backtests that used STOP_LIMIT orders | F1 | buy stop-limits could fill below their trigger (optimistic); rerun them |
| Live account `live_angelone_100000_r2`, 2026-11-23 | F4 (after the 11:47 and 12:36 restarts) | some processed minutes may be recorded as `no_data`: missing equity points on the "Account value through the day" chart and in `what-if` end values; trades are unaffected. Run `ledger-audit` to see exact minutes |
| Any live ledger with a logged "cycle error" | F2 | trades in a skipped minute are missing; `ledger-audit` lists minutes with bars but no cycle row |
| `trade-report` / `trade-rca` for days with overnight (swing) exits | F6 | entry time and buy charges missing; rerun. 2026-11-23 had no overnight exits |
| Accounts continued twice (`..._v11` -> `..._v12` -> next) | F5 | inherited evidence rebuilt automatically at the next migration |
| Dashboard while a replay ran during live trading | F3 | display only; no ledger effect |

## 2. Challenger `orb30-breadth-1.0`

`services/screening/challenger.py`; parameters in the frozen `ChallengerConfig` (not tuned on any evaluation
period). It uses the incumbent's scheduler, ledger, feature engine, `Executor` (next-bar fills, participation
cap, spread, impact, entry time-to-live, partial fills), dated NSE charges, portfolio limits, daily loss limit
and 15:15 square-off.

| Rule | Value |
|---|---|
| Universe | same eligible equity watchlist as the incumbent; indices never traded |
| Opening range | first 30 one-minute bars of the session; a symbol missing any of them is skipped that day |
| Entry | first completed 5-minute close after the opening range above the OR high, when breadth > 50% |
| Breadth | of watchlist stocks with a bar within 2 minutes, the share above their own session open; used only if that fresh-data coverage is >= 80% of the watchlist (frozen) |
| Order | submitted after the signal; earliest fill the next bar, through the shared execution model |
| Stop | OR low on the 1-minute low; the exit fills at the next bar's open, so a gap through the stop executes at the worse, available price |
| Size | shares = floor(0.25% x equity / (signal close - OR low)), then the incumbent's per-trade, cash, per-symbol, gross, positions and daily-loss limits |
| Frequency | one entry decision per symbol per day, no averaging down, no entries after 15:00, square-off from 15:15, nothing held overnight (residual positions are reported) |
| Risk ceilings | `PortfolioRules.live(capital)` (the incumbent's), except the incumbent's strategy-selection gate (`group_gate_trades`), which is a selection policy, not a risk limit |

## 3. Contest protocol

`services/screening/contest.py`.

* **Isolation and identical inputs.** Two accounts in separate folders and ledgers, identical starting capital,
  watchlist, timestamps and risk ceilings. In replays both are fed the same bars at the same processing time;
  live, the challenger is a *follower* of the live scheduler and ingests exactly the bars the incumbent fetched,
  every minute, whether or not either trades (`MinuteScheduler.followers`). Cash (0%) is the third reference.
* **Chronology and freezing.** Development sessions first (implementation check, nothing tuned). Then
  `manifest.json` is frozen: code commit (and whether the tree had uncommitted changes), incumbent and
  challenger versions and parameters, execution costs, data source and a SHA-256 fingerprint of the evaluation
  bars, development and evaluation dates, the primary measure and verdict rules. Its SHA-256 is stored in the
  report; freezing a different manifest into the same folder is refused.
* **Primary measure** (declared before results): paired daily after-cost portfolio return difference,
  challenger minus incumbent, per session, including zero-trade sessions. 95% circular block bootstrap of the
  mean (block 5 sessions, 10,000 resamples, seed 20261124).
* **Also reported for both:** cumulative net P&L (rupees), maximum drawdown, worst day, turnover, total charges,
  closed trades, average exposure, partial and cancelled orders, buys refused by limits, residual positions.
* **Verdict (forward paper only).** At least 60 completed paper sessions AND 100 closed challenger trades.
  "challenger ahead" only if the interval's lower bound > 0, the challenger's cumulative net P&L > 0 and its
  maximum drawdown is no worse than the incumbent's; "incumbent ahead" if the upper bound < 0; otherwise
  "inconclusive". Synthetic and historical replays are labelled and never produce a forward verdict. A rule
  change is a new challenger version, a new account folder and a new forward evaluation.

## 4. Commands

From a clean checkout (after `pip install -e .`):

```bash
python -m nseql.cli contest --fixture
```

Synthetic development (5 sessions) and holdout (15 sessions) on 10 symbols; prints both accounts, cash, the
paired measure and the verdict; writes `experiments/contest/<run>/report.json` and `manifest.json`. Options:
`--symbols`, `--dev`, `--eval`, `--capital`, `--seed`, `--out`.

```bash
python -m nseql.cli contest --dataset <id>
```

The same protocol on a stored dataset's 1-minute bars (historical real-data replay when the dataset is not
synthetic).

```bash
python -m nseql.cli ledger-audit --account live_angelone_100000_r2
```

Live forward contest: with `NSEQL_LIVE_SCREENING=angelone` the challenger account
`<live account>_challenger_orb30-breadth-1_0` starts beside the live bot (disable with `NSEQL_CHALLENGER=off`);
sessions count from the first full session after it started. Dashboard: **Strategies → Incumbent vs
Challenger** (`/contest`, API `/api/contest`).

## 5. Limitations

* No licensed historical 1-minute data is in this repository and no broker credentials are available to the
  build environment: the **real-data verdict is pending**. Verified here: implementation and synthetic runs.
* Synthetic minute data has no real opening-range or breadth behaviour; synthetic results say nothing about
  whether the challenger works.
* The forward contest needs at least 60 sessions (about three months) before it can say anything.
* Breadth is measured on the watchlist (about 20 stocks), not the whole market.

## 6. Robustness checks (before trusting any good-looking result)

`services/screening/robustness.py`, run with:

```bash
python -m nseql.cli robustness --fixture
```

Options: `--dataset <id>` (historical replay), `--account <folder>` (Monte Carlo only, on a live paper account's closed
trades), `--symbols`, `--days`, `--window`, `--workers`, `--no-incumbent`. Report: `experiments/robustness/<run>/report.json`,
shown on the Incumbent vs Challenger page.

| Check | What it does | Flag |
|---|---|---|
| 1. Monte Carlo trade shuffle | the closed trades (net after costs) are reshuffled 1,000 times (seed 20261124); each order's drawdown from the starting capital. Total P&L never changes, only the sequence | sequence risk if the 95th-percentile drawdown is at least double the actual one: expect it live eventually |
| 2. Walk-forward | grid of 9 challenger variants (opening range 15/30/45 min x breadth 40/50/60%); the best on each window of N sessions is scored on the next N sessions it has never seen; the frozen default is scored out-of-sample too | "memory, not edge" if the mean out-of-sample result is <= 0 or less than half the in-sample result |
| 3. Parameter sensitivity | each challenger parameter nudged one step either side (opening range 25/35, breadth 45/55%, coverage 70/90%, risk 0.20/0.30%, freshness 1/3 min) | plateau only if every neighbour is positive and keeps at least half of the baseline; else "lonely spike"; a losing baseline is "no edge to test" |

The incumbent runs thousands of fixed rules without a parameter search, so walk-forward and sensitivity apply to the
challenger (the backtester's own walk-forward is in the Walk-Forward workspace); the Monte Carlo check runs on both.
Synthetic results test the machinery, not the market.
