# One-minute screening loop

`nseql.screening` (in `services/screening/`) is the first working slice of the multi-horizon
platform. It is a central scheduler that runs a cycle after every minute of the trading session. Each
cycle updates one shared feature pipeline, evaluates the eligible configurations among up to 50,000,
and writes decisions and simulated orders to a forward paper ledger. It covers **intraday** (1m, 5m,
15m) and **swing** (daily) strategies on cash equities. No broker is ever called.

## How a minute is processed

| Step | What happens |
|---|---|
| Ingest | 1-minute bars go into a durable event log keyed by `(symbol, minute)`. Duplicates are ignored. A copy with different values is quarantined (first value wins). Bars for a minute already processed are marked late and not applied. |
| Wait | A minute is processed once its close + `grace_seconds` (5 s by default) has passed. Missed minutes are processed in order (catch-up). Session minutes with no data become `no_data` cycles. |
| Fills | Simulated orders decided earlier fill at the open of the symbol's bar for this minute. Charges come from the dated schedule (MIS intraday, CNC delivery for swing). |
| Features | 1m bars are aggregated into 5m, 15m and daily bars. SMA 5…200, prior 10/20/55-bar high and low, and RSI 2/5/14 are updated only for timeframes whose bar closed, and only for symbols with data. |
| Eligibility | A configuration is evaluated if its timeframe closed and its symbol has a new bar; if it holds a position (stop check every minute against the 1m low); or if it is intraday and holds a position after 15:15 (square-off). No intraday entries after 15:00. |
| Decisions | Evaluated all at once with array operations. The slice emits BUY, EXIT, HOLD and NO TRADE; SELL, ACCUMULATE, REDUCE and REBALANCE are defined for products not yet built. Actionable decisions become simulated orders with deterministic keys. |
| Checkpoint | Engine state is saved every 15 minutes and at the close, by atomic replace. |

**Idempotency.** The engine refuses any minute at or before the last one it processed, so repeating an
event cannot evaluate a configuration twice. Signal and order rows carry deterministic keys and are
inserted with `OR IGNORE`.

**Recovery.** On start, the checkpoint is loaded and every logged minute after it is replayed through the
same deterministic engine.

**Execution and money.** Two execution models exist (`services/screening/execution.py`); scheduled runs,
the API replay and the benchmark use the realistic one by default.

- *simple*: whole order at the next minute's open, Rs 1,00,000 per configuration in its own sub-account.
  Kept because the parity proof below is defined on it.
- *realistic*, with one shared portfolio:
  - **Liquidity:** all our orders in a symbol together may take at most 10% of each 1-minute bar's
    volume. Exits are served before entries, then earlier decisions, then registry order, so replays are
    deterministic.
  - **Price:** the open, worse by a 5 bps half-spread plus impact of 25 bps × √(qty / bar volume),
    rounded to the 0.05 tick against us.
  - **Partial fills:** an entry still incomplete after 5 minutes keeps what filled and cancels the rest.
    An exit keeps working until flat, overnight if it must.
  - **Charges:** per fill, with brokerage capped per order across its fills and the depository charge
    once per sell order.
  - **One capital pot** (default Rs 10,00,000):
    - each BUY is sized at 2% of equity;
    - it becomes an order only if cash minus reservations stays ≥ 0, the symbol stays ≤ 10% of equity
      across all configurations, gross exposure stays ≤ 100% (no leverage), and open plus pending
      positions stay < 50;
    - if equity falls 2% below the day's open, no new entries for the rest of the day.
  - **Refused BUYs:** counted per minute by reason in `refusals`; the signal itself stays in `signals`.
  - **Ranking (since 2026-11-21, `allocation="ranked"`, the default):** competing BUYs in a minute are
    served best-first by a score that uses only information available at that minute; see
    "Ranked allocation" below. `allocation="first_come"` keeps the old registry-order baseline.

## Rule families (since 2026-11-23: 36 rules in 12 families, registry version 2)

| Family | Entry | Exit | Timeframes |
|---|---|---|---|
| cross | SMA fast above SMA slow (5 pairs) | no longer above | all |
| breakout | close above prior n-bar high (5) | below prior n-bar low | all |
| rsi | RSI below buy level (9) | above sell level | all |
| zscore | z-score of close vs 20-bar mean below -1.5 / -2 / -2.5 | z above 0 | all |
| bollinger | close above upper band (20, 2 sd) | below the 20-bar mean | all |
| macd | MACD above signal (12-26-9, 8-17-9) | below signal | all |
| momentum | 10 / 20 / 55-bar return above 0 / 0.5% / 1% | return below 0 | all |
| volume | new 10 / 20-bar high on > 1.5x the 20-bar mean volume | below 10-bar low | all |
| vwap | close 0.3% / 0.6% below session VWAP | back above VWAP | intraday |
| orb | above the 09:16-09:30 opening-range high | below the range middle | intraday |
| gap | gap-and-go (> 0.5% gap up, above the open) / gap-fill (> 0.5% gap down, still below) | below the open / gap filled | intraday |
| prevday | above yesterday's high | 0.5% back below it | intraday |

306 configurations per symbol (30 rules x 9 + 6 session rules x 6). Every new indicator has an
independent pandas reference; the parity tests check features and every decision for all 12 families.
Measured on a synthetic session: 6,426 configurations p99 8 ms per minute, 50,000 p99 49 ms. A saved
run or live paper account from registry version 1 is not resumed: live accounts get a new folder
(`live_angelone[_capital]_r2`) and an old replay is reported as outdated. The benchmark tables below
were measured with the 3-family registry.

## Registry

Per symbol there are 19 rules (5 SMA crosses, 5 breakouts, 9 RSI) × (3 intraday timeframes × 2 stops +
1 daily timeframe × 3 stops), which gives 171 configurations. 300 symbols give 51,300; the first N by id
are registered. Ids are a hash of family, timeframe, parameters, symbol and stop, so the same
configuration has the same id everywhere.

## Correctness evidence (tests/screening)

| Test | What it shows |
|---|---|
| Feature parity | Incremental features equal an independent pandas implementation at every bar close, across all four timeframes, including symbols with missing minutes (>3,000 comparisons). |
| Decision parity | About 17,000 BUY/EXIT decisions from the streaming engine equal a per-configuration reference state machine run on batch features (12 sessions, 342 configurations). This test found a real defect: on flat prices two moving averages are mathematically equal, and rounding flipped the decision. Rule comparisons now require "greater" beyond a relative 1e-9. |
| Duplicates | The same bars delivered again, the same cycle run twice, and an old minute fed to the engine change nothing: no new evaluations, signals, orders or cycles. |
| Crash and restart | A run stopped after minute 1,234 without a final checkpoint, then restarted from the checkpoint with the feed starting from the beginning, gives a ledger and state identical to an uninterrupted run. |
| Gaps, late data, conflicts, kill switch | Gaps become `no_data` minutes. Late and conflicting bars are quarantined. With the kill switch engaged, screening continues and no simulated orders are created. |
| Clock | The background thread runs a cycle after the minute boundary, with no overruns. |

## Realistic execution and shared portfolio: evidence (tests/screening/test_execution_portfolio.py)

| Test | What it shows |
|---|---|
| Volume cap | Over 3 sessions, 40 symbols and 6,840 configurations, no symbol-minute had fills above 10% of that bar's volume. |
| Prices | Every fill is worse than the open by exactly the half-spread plus impact, rounded against us to the tick. |
| Cash | Final cash equals capital − buys − charges + sells, to the paisa. Free cash never went below zero (no rupee committed twice). Open plus pending positions never exceeded 50. Positions match the fill ledger. |
| Refusals | Every BUY signal is either an order or a counted refusal. |
| Partial fills | On a thin symbol (4 shares per minute) orders split over several bars; entries time out after 5 minutes keeping what filled; exits are never abandoned. |
| Daily loss limit | A 6% afternoon slide trips the limit. No new entries follow that day, and entries resume the next day. |
| Recovery | A crash and restart gives fills, orders, refusals, equity and cash identical to the uninterrupted run. A checkpoint from the other execution model is refused. |

**What it showed on synthetic data.** With costs and a shared Rs 10 lakh pot, the 1-minute configurations
churned enough to hit the 2% daily loss limit by about 11:00 on each of 3 sessions (equity −6.4% over the
run, mostly spread, impact and charges). The portfolio refused about 99% of BUY signals: daily loss
limit, then the 50-position cap. This is random-walk data, so it says nothing about real markets. It
does show the old per-configuration view (every signal tradeable, Rs 1 lakh each, filled at the open)
was far too generous.

## Benchmark (2026-11-20, realistic execution + shared portfolio, the default)

Same hardware, data and command as below; `--execution realistic`. Full results:
[benchmarks/screening_realistic_2026-11-20.json](benchmarks/screening_realistic_2026-11-20.json).

| Configurations | 1,000 | 5,000 | 10,000 | 50,000 |
|---|---:|---:|---:|---:|
| Cycle time p50 / p95 / p99 (ms) | 8 / 22 / 31 | 22 / 39 / 50 | 27 / 53 / 67 | 91 / 173 / 249 |
| Cycle time max (ms) | 56 | 59 | 289 | 1,579 |
| Engine only p99 (ms) | 6.2 | 7.8 | 8.4 | 19.2 |
| Missed evaluations / cycles over 60 s | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Peak memory (MB) | 276 | 277 | 278 | 293 |
| BUY/EXIT signals | 40,894 | 261,794 | 528,899 | 2,764,540 |
| Signals refused by the portfolio | 38,404 | 259,300 | 526,405 | 2,762,046 |
| Orders / fills | 2,490 / 2,548 | 2,494 / 2,552 | 2,494 / 2,552 | 2,494 / 2,552 |
| Ledger database after 3 sessions (MB) | 60 | 95 | 138 | 494 |
| Ledger identical after SIGKILL + restart | yes | yes | yes | yes |

### Two things the numbers expose

- **Capital went to whoever asked first** (in this 2026-11-20 run). From 5,000 configurations up, the
  portfolio placed exactly the same orders: the first configurations in registry order took the 50 slots
  and the 2% daily loss limit stopped entries early. This is what the ranked allocator (2026-11-21)
  replaces; the benchmark has not been re-run with it.
- **Refused strategies keep asking.** A refused configuration stays flat and signals BUY again on every
  bar while its condition holds. That is why there are 2.76 million signals at 50k, and why the database
  reaches 494 MB per 3 sessions. Storing only the first signal of each refused streak is the obvious fix;
  it is not done yet.

## Benchmark (2026-11-19, simple execution)

Hardware: Intel Xeon @ 2.10 GHz, 4 logical CPUs, 15.7 GB RAM; Python 3.13.18, NumPy 2.5.5, one process.

Data: **SYNTHETIC** random-walk 1-minute bars for 300 symbols (2% of symbol-minutes missing) over 3
sessions, i.e. 1,125 minutes and about 330,000 bars. Not market data.

Command: `python scripts/bench_screening.py`. Full results:
[benchmarks/screening_simple_2026-11-19.json](benchmarks/screening_simple_2026-11-19.json).

| Configurations | 1,000 | 5,000 | 10,000 | 50,000 |
|---|---:|---:|---:|---:|
| Evaluations (3 sessions) | 287,145 | 1,543,852 | 3,086,817 | 15,648,707 |
| Cycle time p50 / p95 / p99 (ms) | 12 / 37 / 48 | 22 / 45 / 57 | 30 / 54 / 74 | 85 / 162 / 211 |
| Cycle time max (ms) | 58 | 81 | 115 | 421 |
| Engine only p50 / p99 (ms) | 2.9 / 9.6 | 3.7 / 10.3 | 4.6 / 13.2 | 14.6 / 34.1 |
| Cycles over the 60 s budget | 0 | 0 | 0 | 0 |
| Minutes processed / minutes with data | 1,125 / 1,125 | 1,125 / 1,125 | 1,125 / 1,125 | 1,125 / 1,125 |
| Eligible but not evaluated | 0 | 0 | 0 | 0 |
| Peak resident memory (MB) | 275 | 276 | 277 | 291 |
| CPU seconds / wall seconds | 14 / 20 | 17 / 26 | 21 / 33 | 63 / 102 |
| Ledger database after 3 sessions (MB) | 58 | 81 | 110 | 350 |
| Checkpoint (MB) | 2.3 | 2.8 | 3.4 | 8.0 |
| Kill at minute 682 → restart: checkpoint load (s) | 0.013 | 0.016 | 0.022 | 0.019 |
| Ledger identical to an uninterrupted run | yes | yes | yes | yes |

*Cycle time* covers ingesting the minute's bars, evaluating, and writing the ledger. *Engine only* is
the evaluation step.

### Wall-clock run

The 50,000 registry ran on the real clock for 3 minutes, using the background thread and a feed that
delivers each minute's bars at its close. Decisions were written **2.01 s after each minute's close**:
the 2 s grace period plus 5–9 ms of processing. There were no overruns. These were the session's first
minutes, when few timeframes close; the heaviest minute (15:30, when every timeframe closes) appears
only in the replay benchmark, at 421 ms.

### What the numbers mean, and what they do not

- **Headroom.** At 50,000 configurations the worst minute took 0.42 s of a 60 s budget, in one process.
  Parallel or distributed workers are not justified at this load and were not built.
- **Memory** is dominated by the Python runtime and the synthetic stream the benchmark holds in memory.
  The engine state is about 8 MB at 50k (the checkpoint size).
- **Storage grows quickly.** The random walk makes rules flip often: 790,683 actionable signals in 3
  sessions at 50k, about 117 MB of SQLite per session. Real data will differ, but retention or an
  analytical store is needed before running for months.
- **Recovery.** After `SIGKILL`, a restart takes about 0.4–0.7 s, including building the registry. It
  loads the last checkpoint (7 minutes behind the kill point at a 15-minute interval) and replays
  logged minutes as the feed resumes. The final ledger is byte-identical to an uninterrupted run at
  every size.
- **Freshness** in the replay benchmark is processing time only. Freshness against a real feed (feed
  delay, jitter, late bars) is **not measured**, because no licensed real-time feed is connected.
- **Not measured:** multi-process scaling, behaviour under real exchange load, and cloud cost. The
  benchmark ran on one 4-vCPU, 16 GB machine.

## Ranked allocation (services/screening/allocator.py)

Inputs, all known at the decision minute (no future returns):

| input | how |
|---|---|
| forward evidence | each configuration's completed round trips in THIS paper ledger (net return after charges and simulated slippage) |
| shrinkage / confidence | mean and win rate pulled toward the family+timeframe group (k=10), the group toward 0 / 50% (k=20); confidence n/(n+10); "qualified" = >= 5 trades and positive shrunk mean |
| calibration | predicted win probability at entry vs outcome; after 30 resolved, a reliability factor in [0.5, 1] from the Brier score vs the base rate scales the evidence |
| costs, liquidity | minus 2x impact for this order size at the EWMA 1-minute volume; size cut to participation x volume x TTL; below one share = rejected |
| correlation, redundancy | minus 10 bps x correlation of the symbol's last 60 one-minute returns with the portfolio; one new entry per symbol per minute |
| diversification | minus 10 bps x the symbol's share of its limit, minus 5 bps x the family's share of open positions |
| risk limits | then the existing hard limits (cash, symbol, gross, positions, daily loss) in rank order |
| negative expectancy | a configuration with >= 5 forward trades and negative expected net return gets no capital |

Outcomes per BUY (`signals.alloc_status`, `alloc_reason`, `alloc_score`): **accepted**; **rejected** (fails
on its own, or a limit was already full before the minute); **deferred** (would have passed, but a
higher-ranked signal used the capital / slot / symbol room). Allocator state is in the checkpoint.

### Comparison with first-come on unseen data (docs/benchmarks/allocator_comparison_2026-11-21.json)

20 symbols (3,420 configurations), 2 warm-up days then 3 evaluation days, seeds 201-205 (not used in
development; seed 101 was looked at once before the negative-expectancy gate was added, so it was
dropped from the evaluation). SYNTHETIC data.

| scenario | ranked better | mean difference (3 days) | both allocators |
|---|---|---|---|
| random walk | 4 of 5 seeds | +Rs 1,898 | lose Rs 45k-69k |
| common factor + persistent edges (many simultaneous BUYs; busiest minute 550-900) | 5 of 5 | +Rs 4,143 | lose Rs 45k-69k |

Ranking reduces losses modestly; it does not make these strategies profitable. Most intraday
configurations lose to costs; with ~20% win rates almost none qualify within two days.

## Poller capacity (docs/benchmarks/poller_capacity_2026-11-21.json, MODELLED)

One request per symbol per cycle, 0.4 s spacing, 50 s budget: at most 125 symbols every minute. Larger
watchlists are polled in rotation (250 symbols: every 2 minutes; 500: every 4; 1,000: every 8), reported
as degraded in `feed_health`. The fetch runs inside the cycle, so at 125 symbols bars are processed ~50 s
after the minute. These are simulated-clock numbers; real latency and limits need `nseql angel-verify`.

## Not in this slice

- A verified live feed. Angel One polling exists (`nseql.screening.feeds`, automatic login, warm-up with paper orders off) but is **unverified** against the real service; KiteTicker is not implemented.
- Positional (weekly/monthly) and long-term investing.
- F&O, ETFs, mutual funds and SIP.
- Shared portfolio capital.
- Short selling.
- Volume caps in the paper loop.
- Use of the Learning Brain's memory in live screening.

See [PLATFORM_V5_STATUS.md](PLATFORM_V5_STATUS.md).
